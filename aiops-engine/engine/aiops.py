"""AIOps engine main loop.

Every LOOP_INTERVAL seconds:
 1. collect correlated signals (metrics, logs, traces topology, health, config)
 2. ML: anomaly detection, baseline/drift, pattern, LSTM failure prediction
 3. capacity forecast + predictive maintenance plan
 4. root cause analysis -> incidents (priority, dedup, correlation)
 5. decision controller -> auto-remediate / human approval / notify / schedule
 6. compliance validation, SLA indicators, metrics, audit trail
"""
import json
import logging
import os
import sys
import time

import config
from engine import alerting, api, capacity, compliance, decision, metrics, ml, rca, remediation, signals, state
from engine.audit import audit, now_iso

os.makedirs(config.LOG_DIR, exist_ok=True)
logger = logging.getLogger("aiops")
logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
if not logger.handlers:
    fmt = logging.Formatter("%(message)s")
    for h in (logging.FileHandler(config.LOG_PATH), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)
logger.propagate = False

ctx = {"cpu_high_cycles": 0, "cpu_low_cycles": 0, "latency_high_cycles": 0, "anomaly_cycles": 0, "scaled_level": 0}
RISK = {"NORMAL": 0, "MEDIUM_RISK": 1, "HIGH_RISK": 2}


def _sla(s):
    av = s.get("availability_1h")
    p95 = s.get("p95_1h")
    allowed = 100.0 - config.SLO_AVAILABILITY
    budget = None if av is None else max(-100.0, round(100.0 * (1 - (100.0 - av) / allowed), 1))
    compliant = (av is None or av >= config.SLO_AVAILABILITY) and (p95 is None or p95 <= config.SLO_P95_LATENCY)
    return {"availability_pct": av, "p95_latency_s": p95, "error_budget_remaining_pct": budget,
            "slo_availability": config.SLO_AVAILABILITY, "slo_p95_s": config.SLO_P95_LATENCY, "compliant": compliant,
            "window": config.SLA_WINDOW}


def _update_ctx(s, ml_out):
    ctx["cpu_high_cycles"] = ctx["cpu_high_cycles"] + 1 if s["cpu_util"] >= config.SLA["cpu"] else 0
    ctx["cpu_low_cycles"] = ctx["cpu_low_cycles"] + 1 if s["cpu_util"] < 30 else 0
    ctx["latency_high_cycles"] = ctx["latency_high_cycles"] + 1 if s["p95_latency"] > config.SLO_P95_LATENCY else 0
    ctx["anomaly_cycles"] = ctx["anomaly_cycles"] + 1 if ml_out.get("anomaly") else 0
    ctx["scaled_level"] = state.desired.get("level", 0)


def _export(s, ml_out, cap, sla, health, maint):
    metrics.ANOMALY.set(1 if ml_out.get("anomaly") and ctx["anomaly_cycles"] >= 2 else 0)
    metrics.ANOMALY_SCORE.set(ml_out.get("score") or 0)
    b = ml_out.get("baseline") or {}
    metrics.MAX_Z.set(b.get("max_z") or 0)
    metrics.DRIFT.set(b.get("drift_score") or 0)
    metrics.CPU_UTIL.set(s["cpu_util"])
    metrics.CPU_PRED.set(ml_out.get("predicted_cpu") or 0)
    metrics.CAP_FORECAST.set(cap.get("forecast_cpu_util") or 0)
    metrics.FAILURE_RISK.set(RISK.get(ml_out.get("failure_risk"), 0))
    metrics.HEALTH.set(health)
    if sla["availability_pct"] is not None:
        metrics.SLA_AVAIL.set(sla["availability_pct"])
    if sla["error_budget_remaining_pct"] is not None:
        metrics.ERROR_BUDGET.set(sla["error_budget_remaining_pct"])
    metrics.SLA_COMPLIANT.set(1 if sla["compliant"] else 0)
    metrics.MEM_MINUTES.set(s["mem_minutes_to_limit"] if s.get("mem_minutes_to_limit") is not None else -1)
    metrics.DISK_HOURS.set(s["disk_hours_to_full"] if s.get("disk_hours_to_full") is not None else -1)
    opn = alerting.open_incidents()
    for p in ("P1", "P2", "P3", "P4"):
        metrics.INCIDENTS_OPEN.labels(p).set(sum(1 for i in opn if i["priority"] == p))
    metrics.MTTR.set(alerting.mttr())
    metrics.NOISE.set(alerting.noise_reduction_ratio())
    metrics.SCALE_LEVEL.set(state.desired.get("level", 0))
    metrics.MODEL_TRAINED.set(1 if ml_out.get("trained") else 0)
    metrics.TRAINING_RUNS.set(ml.train_count)


def cycle():
    s = signals.collect()
    if s["app_up"] and s["health_code"] == 200:
        ml_out = ml.observe(signals.feature_vector(s))
    else:  # do not train the models on outage data (rules handle outages)
        ml_out = {"trained": ml.trained, "samples": len(ml.history), "anomaly": False, "skipped": "app unavailable",
                  "failure_risk": "UNKNOWN", "baseline": {}}
    _update_ctx(s, ml_out)
    ml_out["confirmed_anomaly"] = bool(ml_out.get("anomaly") and ctx["anomaly_cycles"] >= 2)

    cap = capacity.predict_capacity()
    maint = capacity.maintenance_plan(s)
    health = capacity.health_score(s, ml_out)
    sla = _sla(s)
    comp = compliance.check()

    hyps = rca.analyze(s, dict(ml_out, anomaly=ml_out["confirmed_anomaly"]), cap, ctx)
    dh = compliance.drift_hypothesis(comp)
    if dh:
        hyps.insert(0, dh)
    new = alerting.process(hyps, s)
    for inc in new:
        metrics.INCIDENTS.labels(inc["priority"], inc["rca"]).inc()

    last_decision = {}
    for inc in alerting.open_incidents():
        if inc["status"] != "OPEN":
            continue
        d = decision.decide(inc, s)
        changed = (inc.get("decision") or {}).get("mode") != d["mode"]
        inc["decision"] = d
        metrics.CONFIDENCE.set(inc["confidence"])
        last_decision = dict(d, incident=inc["id"])
        if d["mode"] == "AUTO":
            audit("DECISION", incident=inc["id"], rca=inc["rca"], action=d["action"], mode="AUTO", reason=d["reason"])
            remediation.start(inc, d["action"])
        elif changed:
            audit("DECISION", incident=inc["id"], rca=inc["rca"], action=d["action"], mode=d["mode"], reason=d["reason"],
                  recommendation=d["recommendation"])
            if d["mode"] == "MANUAL_APPROVAL":
                metrics.ESCALATIONS.labels("manual_approval").inc()
                alerting.escalate(inc, d["reason"])

    state.update(signals=s, ml=ml_out, capacity=cap, maintenance=maint, sla=sla, health_score=health,
                 decision=last_decision or state.snapshot.get("decision", {}))
    _export(s, ml_out, cap, sla, health, maint)

    top = hyps[0] if hyps else {}
    logger.info(json.dumps({
        "type": "aiops_event", "ts": now_iso(), "cpu": s["cpu_util"], "latency": s["avg_latency"],
        "p95": s["p95_latency"], "requests": s["rps"], "error_rate": s["error_pct"], "log_errors": s["log_errors_1m"],
        "anomaly": ml_out.get("confirmed_anomaly"), "score": ml_out.get("score"), "pattern": ml_out.get("pattern"),
        "failure": ml_out.get("failure_risk"), "predicted_cpu": ml_out.get("predicted_cpu"),
        "capacity": cap.get("status"), "rca": top.get("rca", "NONE"), "confidence": top.get("confidence"),
        "decision": (last_decision or {}).get("action", "NO_ACTION"), "mode": (last_decision or {}).get("mode"),
        "open_incidents": len(alerting.open_incidents()), "sla": sla["availability_pct"], "health": health,
    }, default=str))


def run():
    api.serve()
    state.update(started_at=now_iso())
    audit("ENGINE_STARTED", config={"min_confidence": config.AUTO_REMEDIATE_MIN_CONFIDENCE,
                                    "slo_availability": config.SLO_AVAILABILITY, "slo_p95": config.SLO_P95_LATENCY,
                                    "dry_run": config.DRY_RUN})
    logger.info("AIOps Engine Running (loop %ss)", config.LOOP_INTERVAL)
    while True:
        t0 = time.time()
        try:
            cycle()
        except Exception as e:
            logger.exception("Engine loop error: %s", e)
        time.sleep(max(0.5, config.LOOP_INTERVAL - (time.time() - t0)))
