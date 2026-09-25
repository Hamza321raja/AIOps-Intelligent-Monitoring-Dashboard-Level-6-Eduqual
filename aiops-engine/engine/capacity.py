"""Capacity forecasting, scaling recommendations and predictive maintenance
scheduling."""
from datetime import datetime, timedelta, timezone

import numpy as np

import config
from engine.ml import history


def predict_capacity(horizon=12):
    """Linear-trend forecast of CPU utilisation and traffic `horizon` cycles ahead
    (12 x 5s = 1 minute) with a scaling recommendation."""
    if len(history) < 20:
        return {"status": "UNKNOWN", "reason": "Not enough data"}
    H = np.asarray(list(history)[-120:], dtype=float)
    t = np.arange(len(H))
    cpu_slope = np.polyfit(t, H[:, 0], 1)[0]
    req_slope = np.polyfit(t, H[:, 2], 1)[0]
    f_cpu = float(H[-1, 0] + cpu_slope * horizon)
    f_req = float(max(0.0, H[-1, 2] + req_slope * horizon))
    res = {"forecast_cpu_util": round(f_cpu, 2), "forecast_rps": round(f_req, 2),
           "cpu_trend_per_min": round(float(cpu_slope * 12), 2), "horizon_s": horizon * config.LOOP_INTERVAL}
    if f_cpu > 85:
        res.update(status="SCALE_UP", recommendation="Forecast CPU %.0f%% of limit: add CPU/memory or replicas" % f_cpu)
    elif f_cpu < 25 and H[-12:, 0].mean() < 25:
        res.update(status="SCALE_DOWN", recommendation="Sustained low utilisation: right-size to baseline")
    else:
        res.update(status="STABLE", recommendation="No scaling required")
    return res


def _next_window(before=None):
    """Next maintenance window (daily at MAINTENANCE_HOUR_UTC), or earlier if the
    predicted failure would happen first."""
    now = datetime.now(timezone.utc)
    w = now.replace(hour=config.MAINTENANCE_HOUR_UTC, minute=0, second=0, microsecond=0)
    if w <= now:
        w += timedelta(days=1)
    if before is not None and before < w:
        w = max(now + timedelta(minutes=1), before - timedelta(minutes=15))
    return w.strftime("%Y-%m-%dT%H:%M:%SZ")


def maintenance_plan(s):
    """Predictive maintenance: converts resource-exhaustion forecasts into a
    maintenance schedule with a risk level."""
    plan = []
    now = datetime.now(timezone.utc)

    m = s.get("mem_minutes_to_limit")
    if m is not None and m < config.MEMORY_RISK_MINUTES * 4:
        eta = now + timedelta(minutes=m)
        risk = "HIGH" if m < config.MEMORY_RISK_MINUTES else "MEDIUM"
        plan.append({"component": "app (memory)", "risk": risk,
                     "predicted_failure_in_min": round(m, 1),
                     "evidence": "process memory %.0f/%.0f MB, linear growth trend" % (s.get("mem_mb", 0), s.get("mem_limit_mb", 0)),
                     "action": "PREEMPTIVE_RESTART", "window": "IMMEDIATE" if m < 10 else _next_window(eta)})

    d = s.get("disk_hours_to_full")
    if d is not None and d < config.DISK_RISK_HOURS * 3:
        eta = now + timedelta(hours=d)
        plan.append({"component": "ec2-host (root disk)", "risk": "HIGH" if d < config.DISK_RISK_HOURS else "MEDIUM",
                     "predicted_failure_in_min": round(d * 60, 0),
                     "evidence": "disk free %.1f%%, linear forecast" % (s.get("disk_free_pct") or 0),
                     "action": "CLEANUP_DISK", "window": _next_window(eta)})

    io = s.get("disk_io_util")
    lat = s.get("disk_latency_ms")
    if (io is not None and io > 80) or (lat is not None and lat > 50):
        plan.append({"component": "ec2-host (disk I/O)", "risk": "MEDIUM",
                     "predicted_failure_in_min": None,
                     "evidence": "disk I/O util %s%%, latency %s ms" % (io, lat),
                     "action": "INVESTIGATE_STORAGE", "window": _next_window()})

    sw = s.get("swap_used_pct")
    if sw is not None and sw > 50:
        plan.append({"component": "ec2-host (memory)", "risk": "MEDIUM", "predicted_failure_in_min": None,
                     "evidence": "swap used %.0f%%" % sw, "action": "RESIZE_INSTANCE", "window": _next_window()})
    return plan


def health_score(s, ml):
    """0-100 infrastructure health score (predictive maintenance KPI)."""
    score = 100.0
    if not s.get("app_up"):
        score -= 50
    score -= min(25, s.get("error_pct", 0) * 1.5)
    score -= min(15, max(0, s.get("cpu_util", 0) - 70) * 0.5)
    if s.get("p95_latency", 0) > config.SLO_P95_LATENCY:
        score -= 10
    if ml.get("failure_risk") == "HIGH_RISK":
        score -= 10
    m = s.get("mem_minutes_to_limit")
    if m is not None and m < config.MEMORY_RISK_MINUTES:
        score -= 10
    if s.get("host_mem_avail_pct") is not None and s["host_mem_avail_pct"] < 10:
        score -= 10
    return round(max(0.0, score), 1)
