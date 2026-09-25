"""Root cause analysis: correlates metrics, logs, health checks, Prometheus
alerts and ML output, maps the root cause onto the service dependency graph
and returns ranked hypotheses with a confidence score."""
import time

import numpy as np
import requests

import config
from engine import state

incident_memory = []          # (normalised feature vector, rca label)
_topology = {"edges": {k: list(v) for k, v in config.DEPENDENCY_MAP.items()}, "t": 0, "jaeger_services": []}

RCA_SERVICE = {
    "SERVICE_DOWN": "app", "APPLICATION_ERROR": "app", "CPU_BOTTLENECK": "app",
    "MEMORY_LEAK": "app", "DEPENDENCY_LATENCY": "app", "PREDICTED_CPU_SATURATION": "app",
    "OVERPROVISIONED": "app", "UNKNOWN_ANOMALY": "app", "PERFORMANCE_DRIFT": "app",
    "EDGE_DOWN": "kubernetes-edge", "EDGE_CPU_HIGH": "kubernetes-edge",
    "HOST_MEMORY_PRESSURE": "ec2-host", "DISK_CAPACITY_RISK": "ec2-host",
    "NETWORK_ISSUE": "network", "CONFIG_DRIFT": "app",
}


def refresh_topology():
    """Merges the static dependency map with service dependencies discovered
    from Jaeger traces (refreshed every 60s)."""
    if time.time() - _topology["t"] < 60:
        return
    _topology["t"] = time.time()
    try:
        svcs = requests.get(config.JAEGER_BASE + "/api/services", timeout=2).json().get("data") or []
        _topology["jaeger_services"] = svcs
        end = int(time.time() * 1000)
        deps = requests.get(config.JAEGER_BASE + "/api/dependencies",
                            params={"endTs": end, "lookback": 3600000}, timeout=2).json().get("data") or []
        for d in deps:
            parent, child = d.get("parent"), d.get("child")
            if parent and child and child not in _topology["edges"].setdefault(parent, []):
                _topology["edges"][parent].append(child)
    except Exception:
        pass


def blast_radius(service):
    """All services that (transitively) depend on `service`."""
    rev = {}
    for src, dsts in _topology["edges"].items():
        for d in dsts:
            rev.setdefault(d, set()).add(src)
    seen, stack = set(), [service]
    while stack:
        for parent in rev.get(stack.pop(), ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return sorted(seen)


def learn_incident(x, label):
    incident_memory.append((np.asarray(x, dtype=float), label))
    del incident_memory[:-200]


def _memory_match(x):
    if len(incident_memory) < 3:
        return None, 0.0
    X = np.array([m[0] for m in incident_memory])
    scale = np.maximum(X.std(axis=0), 1e-6)
    a = (np.asarray(x, dtype=float) - X.mean(axis=0)) / scale
    B = (X - X.mean(axis=0)) / scale
    sims = B @ a / (np.linalg.norm(B, axis=1) * np.linalg.norm(a) + 1e-9)
    i = int(np.argmax(sims))
    return incident_memory[i][1], float(sims[i])


def _firing(names):
    return [a for a in list(state.alerts.values())
            if a.get("status") == "firing" and a.get("labels", {}).get("alertname") in names]


def analyze(s, ml, capacity, ctx):
    """Returns a list of hypotheses sorted by confidence (highest first)."""
    refresh_topology()
    H = []

    def add(rca, conf, evidence):
        svc = RCA_SERVICE.get(rca, "app")
        H.append({"rca": rca, "confidence": round(min(conf, 0.99), 2), "evidence": evidence,
                  "service": svc, "blast_radius": blast_radius(svc)})

    health = s.get("health_code")
    if not s.get("app_up") or health is None:
        ev = ["prometheus up{job=%s}=%s" % (config.APP_JOB, s.get("app_up")), "health check: %s" % (health or "unreachable")]
        conf = 0.9 + (0.05 if _firing(["AppDown"]) else 0)
        add("SERVICE_DOWN", conf, ev + (["alertmanager: AppDown firing"] if _firing(["AppDown"]) else []))
    else:
        if health == 503 or s["error_pct"] >= 20:
            conf, ev = 0.72, ["error rate %.1f%%" % s["error_pct"], "health check HTTP %s" % health]
            if s.get("log_errors_1m", 0) >= 5:
                conf += 0.12
                ev.append("loki: %d application_error log lines/min (log-metric correlation)" % s["log_errors_1m"])
            if _firing(["HighErrorRate", "SLOAvailabilityBreach"]):
                conf += 0.06
                ev.append("alertmanager: error/SLO alert firing")
            add("APPLICATION_ERROR", conf, ev)
        if s["cpu_util"] >= config.SLA["cpu"] and ctx.get("cpu_high_cycles", 0) >= 2:
            conf, ev = 0.7, ["CPU %.0f%% of %.1f-core limit for %d cycles" % (s["cpu_util"], s["cpu_limit"], ctx["cpu_high_cycles"])]
            if s["p95_latency"] > config.SLO_P95_LATENCY * 0.8:
                conf += 0.1
                ev.append("p95 latency %.2fs rising with CPU (resource contention)" % s["p95_latency"])
            if ml.get("anomaly"):
                conf += 0.05
                ev.append("ML anomaly confirmed (IsolationForest + z-score)")
            add("CPU_BOTTLENECK", conf, ev)
        m = s.get("mem_minutes_to_limit")
        if m is not None and m < config.MEMORY_RISK_MINUTES:
            add("MEMORY_LEAK", 0.8 if m < 10 else 0.72,
                ["memory %.0f/%.0f MB, exhausts limit in ~%.0f min (predict_linear)" % (s["mem_mb"], s["mem_limit_mb"], m)])
        if (s["p95_latency"] > config.SLO_P95_LATENCY and s["cpu_util"] < 60 and s["error_pct"] < 5
                and ctx.get("latency_high_cycles", 0) >= 3 and s["rps"] >= 0.5):
            add("DEPENDENCY_LATENCY", 0.55,
                ["p95 latency %.2fs > SLO %.1fs" % (s["p95_latency"], config.SLO_P95_LATENCY),
                 "CPU %.0f%% and errors %.1f%% normal -> slow dependency / I/O suspected" % (s["cpu_util"], s["error_pct"])])
        if (ml.get("failure_risk") == "HIGH_RISK" and capacity.get("status") == "SCALE_UP"
                and s["cpu_util"] >= 60 and not any(h["rca"] == "CPU_BOTTLENECK" for h in H)):
            add("PREDICTED_CPU_SATURATION", 0.74,
                ["LSTM predicts CPU %.0f%%" % (ml.get("predicted_cpu") or 0),
                 "linear forecast %.0f%% (models agree)" % capacity.get("forecast_cpu_util", 0)])
        if ctx.get("scaled_level", 0) > 0 and ctx.get("cpu_low_cycles", 0) >= 12:
            add("OVERPROVISIONED", 0.8, ["CPU %.0f%% for %d cycles at scale level %d" % (s["cpu_util"], ctx["cpu_low_cycles"], ctx["scaled_level"])])

    if s.get("edge_up") == 0:
        add("EDGE_DOWN", 0.8, ["prometheus up{job=kubernetes-edge}=0"])
    elif s.get("edge_cpu") is not None and s["edge_cpu"] > 90:
        add("EDGE_CPU_HIGH", 0.7, ["edge_cpu_usage %.0f%%" % s["edge_cpu"]])
    if s.get("host_mem_avail_pct") is not None and s["host_mem_avail_pct"] < 10:
        add("HOST_MEMORY_PRESSURE", 0.75, ["EC2 host memory available %.1f%%" % s["host_mem_avail_pct"]])
    if s.get("disk_hours_to_full") is not None and s["disk_hours_to_full"] < config.DISK_RISK_HOURS:
        add("DISK_CAPACITY_RISK", 0.8, ["root disk predicted full in %.1f h" % s["disk_hours_to_full"]])
    failed = [k for k, v in (s.get("probes") or {}).items() if v == 0]
    if failed:
        add("NETWORK_ISSUE", 0.6, ["blackbox probe failed: %s" % ", ".join(failed)])

    b = ml.get("baseline", {})
    if b.get("ready") and b.get("drift_score", 0) >= config.DRIFT_THRESHOLD and not H:
        add("PERFORMANCE_DRIFT", 0.5, ["%s drifted %.1f MAD from baseline %s" % (
            b["drift_metric"], b["drift_score"], b["baseline"].get(b["drift_metric"]))])

    if ml.get("anomaly") and not H:
        label, sim = _memory_match([s["cpu_util"], s["avg_latency"], s["rps"], s["error_pct"], s["p95_latency"]])
        if label and sim > 0.85:
            add(label, round(0.5 + 0.2 * sim, 2), ["similar to past incident %s (cosine %.2f)" % (label, sim)])
        else:
            add("UNKNOWN_ANOMALY", 0.4, ["IsolationForest score %.3f, max z %.1f" % (ml.get("score", 0), b.get("max_z", 0))])

    H.sort(key=lambda h: -h["confidence"])
    return H


# backwards compatibility
def ai_rca(x):
    return "UNKNOWN"
