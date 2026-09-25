"""Intelligent alerting and incident management.

* Priority (P1-P4) assignment per root cause and SLA impact
* Fingerprint-based de-duplication: repeated signals update one incident
* 5-minute suppression window for flapping incidents
* Correlation with Prometheus alerts received from Alertmanager
* Incident lifecycle OPEN -> REMEDIATING -> RECOVERING -> RESOLVED (or ESCALATED) with MTTR
"""
import hashlib
import threading
import time

import requests

import config
from engine import state
from engine.audit import audit, now_iso

PRIORITY = {
    "SERVICE_DOWN": "P1", "APPLICATION_ERROR": "P2", "DEPENDENCY_LATENCY": "P2",
    "EDGE_DOWN": "P2", "HOST_MEMORY_PRESSURE": "P2", "CPU_BOTTLENECK": "P3",
    "MEMORY_LEAK": "P3", "PREDICTED_CPU_SATURATION": "P3", "DISK_CAPACITY_RISK": "P3",
    "NETWORK_ISSUE": "P3", "EDGE_CPU_HIGH": "P3", "CONFIG_DRIFT": "P3",
    "UNKNOWN_ANOMALY": "P4", "PERFORMANCE_DRIFT": "P4", "OVERPROVISIONED": "P4",
}
ALERT_SERVICE = {  # which Prometheus alerts belong to which service
    "app": ["AppDown", "HighErrorRate", "HighLatencyP95", "SLOAvailabilityBreach", "AppHighCPU",
            "MemoryExhaustionPredicted", "AIOpsAnomalyDetected", "PerformanceDrift"],
    "kubernetes-edge": ["EdgeDown", "EdgeHighCPU"],
    "ec2-host": ["HostHighCPU", "HostHighMemory", "DiskWillFillIn24h", "HostDiskIOSaturation"],
    "network": ["ProbeFailed", "HighProbeLatency"],
}

RECOVERY_GRACE = 60   # seconds a remediated incident may take to clear
_lock = threading.RLock()
incidents = []          # newest last
_seq = [0]
stats = {"raw_signals": 0, "suppressed": 0, "incidents": 0, "resolved": 0, "escalated": 0,
         "auto_remediated": 0, "mttr_total": 0.0}


def fingerprint(rca, service):
    return hashlib.sha1(("%s|%s" % (rca, service)).encode()).hexdigest()[:12]


def priority_for(h, s):
    p = PRIORITY.get(h["rca"], "P4")
    # SLA impact raises the priority: a 5-minute availability below the SLO is P1
    av = s.get("availability_5m")
    if h["service"] == "app" and av is not None and av < config.SLO_AVAILABILITY and p in ("P2", "P3"):
        p = "P1"
    return p


def correlated_alerts(service):
    names = ALERT_SERVICE.get(service, [])
    return sorted({a["labels"].get("alertname") for a in list(state.alerts.values())
                   if a.get("status") == "firing" and a.get("labels", {}).get("alertname") in names})


def open_incidents():
    with _lock:
        return [i for i in incidents if i["status"] in ("OPEN", "REMEDIATING", "RECOVERING", "ESCALATED")]


def get(incident_id):
    with _lock:
        for i in incidents:
            if i["id"] == incident_id:
                return i
    return None


def process(hypotheses, s):
    """Turns this cycle's hypotheses into incidents. Only the primary hypothesis
    per service becomes an incident; the others are attached as correlated
    evidence (alert correlation / noise reduction)."""
    now = time.time()
    seen = set()
    new = []
    with _lock:
        primary = {}
        for h in hypotheses:
            stats["raw_signals"] += 1
            if h["service"] not in primary:
                primary[h["service"]] = h
            else:
                stats["suppressed"] += 1
                primary[h["service"]].setdefault("secondary", []).append(h["rca"])

        for svc, h in primary.items():
            fp = fingerprint(h["rca"], svc)
            seen.add(fp)
            inc = next((i for i in incidents if i["fingerprint"] == fp and i["status"] != "RESOLVED"), None)
            if inc is None:
                recent = next((i for i in reversed(incidents) if i["fingerprint"] == fp and i["status"] == "RESOLVED"
                               and now - i["resolved_ts"] < config.DEDUP_WINDOW), None)
                if recent is not None:
                    # flapping: re-open the same incident instead of creating a new one
                    recent.update(status="OPEN", resolved_ts=None, resolved_at=None, reopened=recent.get("reopened", 0) + 1)
                    inc = recent
                    stats["suppressed"] += 1
                    audit("INCIDENT_REOPENED", incident=inc["id"], rca=inc["rca"], reason="flapping within dedup window")
            if inc is None:
                _seq[0] += 1
                inc = {"id": "INC-%04d" % _seq[0], "fingerprint": fp, "rca": h["rca"], "service": svc,
                       "priority": priority_for(h, s), "confidence": h["confidence"], "evidence": h["evidence"],
                       "secondary": h.get("secondary", []), "blast_radius": h["blast_radius"],
                       "status": "OPEN", "opened_ts": now, "opened_at": now_iso(), "resolved_ts": None,
                       "resolved_at": None, "occurrences": 0, "actions": [], "missing_cycles": 0,
                       "alerts": correlated_alerts(svc), "decision": None}
                incidents.append(inc)
                del incidents[:-200]
                stats["incidents"] += 1
                new.append(inc)
                audit("INCIDENT_OPENED", incident=inc["id"], rca=inc["rca"], priority=inc["priority"],
                      confidence=inc["confidence"], evidence=inc["evidence"], blast_radius=inc["blast_radius"])
                notify(inc, "opened")
            else:
                stats["suppressed"] += 1
                inc["confidence"] = max(inc["confidence"], h["confidence"])
                inc["evidence"] = h["evidence"]
                p = priority_for(h, s)
                if p < inc["priority"]:
                    inc["priority"] = p
            if inc["status"] == "RECOVERING" and now - inc.get("recovering_ts", now) > RECOVERY_GRACE:
                inc["status"] = "OPEN"
                audit("REMEDIATION_INEFFECTIVE", incident=inc["id"], rca=inc["rca"],
                      reason="symptoms still present %ds after remediation" % RECOVERY_GRACE)
            inc["occurrences"] += 1
            inc["missing_cycles"] = 0
            inc["last_seen_at"] = now_iso()
            inc["alerts"] = sorted(set(inc["alerts"]) | set(correlated_alerts(svc)))

        # resolve incidents whose symptoms disappeared
        for inc in incidents:
            if inc["status"] in ("OPEN", "RECOVERING", "ESCALATED") and inc["fingerprint"] not in seen:
                inc["missing_cycles"] += 1
                if inc["missing_cycles"] >= config.RESOLVE_AFTER_CYCLES:
                    resolve(inc, "symptoms cleared")
    return new


def resolve(inc, reason):
    with _lock:
        if inc["status"] == "RESOLVED":
            return
        was_escalated = inc["status"] == "ESCALATED"
        inc["status"] = "RESOLVED"
        inc["resolved_ts"] = time.time()
        inc["resolved_at"] = now_iso()
        inc["ttr_s"] = round(inc["resolved_ts"] - inc["opened_ts"], 1)
        stats["resolved"] += 1
        stats["mttr_total"] += inc["ttr_s"]
    audit("INCIDENT_RESOLVED", incident=inc["id"], rca=inc["rca"], ttr_s=inc["ttr_s"],
          reason=reason, after_escalation=was_escalated)


def mark(inc, status):
    with _lock:
        inc["status"] = status
        if status == "RECOVERING":
            inc["recovering_ts"] = time.time()


def escalate(inc, reason, priority=None):
    with _lock:
        if priority and priority < inc["priority"]:
            inc["priority"] = priority
        if inc["status"] != "ESCALATED":
            stats["escalated"] += 1
        inc["status"] = "ESCALATED"
        inc["escalation_reason"] = reason
    audit("INCIDENT_ESCALATED", incident=inc["id"], rca=inc["rca"], priority=inc["priority"], reason=reason,
          recommended_action=(inc.get("decision") or {}).get("action"))
    notify(inc, "escalated: " + reason)


def mttr():
    return round(stats["mttr_total"] / stats["resolved"], 1) if stats["resolved"] else 0.0


def noise_reduction_ratio():
    raw = stats["raw_signals"]
    return round(1.0 - stats["incidents"] / raw, 4) if raw else 0.0


def notify(inc, what):
    """Optional Slack / Teams incoming webhook (set NOTIFY_WEBHOOK_URL)."""
    if not config.NOTIFY_WEBHOOK_URL:
        return
    text = "[%s] %s %s on %s (confidence %.2f) - %s" % (inc["priority"], inc["id"], inc["rca"],
                                                       inc["service"], inc["confidence"], what)

    def _send():
        try:
            requests.post(config.NOTIFY_WEBHOOK_URL, json={"text": text}, timeout=5)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


def ingest_alertmanager(payload):
    """Stores alerts pushed by Alertmanager's webhook receiver."""
    n = 0
    for a in payload.get("alerts", []):
        fp = a.get("fingerprint") or fingerprint(a.get("labels", {}).get("alertname", "?"), str(a.get("labels")))
        state.alerts[fp] = {"status": a.get("status"), "labels": a.get("labels", {}),
                            "annotations": a.get("annotations", {}), "startsAt": a.get("startsAt"),
                            "receiver": payload.get("receiver")}
        n += 1
    # drop resolved alerts after a while
    for fp in [k for k, v in state.alerts.items() if v.get("status") == "resolved"][:-50]:
        state.alerts.pop(fp, None)
    return n


# backwards compatibility
def intelligent_alert(*a, **k):
    return None
