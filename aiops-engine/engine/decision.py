"""Decision controller: maps a root cause to an action and decides between
automatic remediation, human approval, notification or scheduled maintenance."""
import config
from engine import remediation
from engine.runbook import RECOMMENDATIONS

ACTION = {
    "SERVICE_DOWN": "RESTART", "APPLICATION_ERROR": "RESTART", "CPU_BOTTLENECK": "SCALE_UP",
    "PREDICTED_CPU_SATURATION": "SCALE_UP", "MEMORY_LEAK": "PREEMPTIVE_RESTART",
    "OVERPROVISIONED": "SCALE_DOWN", "CONFIG_DRIFT": "REVERT_CONFIG",
    "DEPENDENCY_LATENCY": "INVESTIGATE", "EDGE_DOWN": "K8S_SELF_HEAL", "EDGE_CPU_HIGH": "K8S_HPA",
    "HOST_MEMORY_PRESSURE": "RESIZE_INSTANCE", "DISK_CAPACITY_RISK": "SCHEDULE_MAINTENANCE",
    "NETWORK_ISSUE": "INVESTIGATE", "UNKNOWN_ANOMALY": "INVESTIGATE", "PERFORMANCE_DRIFT": "INVESTIGATE",
}
AUTO_ACTIONS = {"RESTART", "PREEMPTIVE_RESTART", "SCALE_UP", "SCALE_DOWN", "REVERT_CONFIG"}


def decide(inc, s):
    rca = inc["rca"]
    action = ACTION.get(rca, "INVESTIGATE")
    d = {"action": action, "rca": rca, "confidence": inc["confidence"],
         "recommendation": RECOMMENDATIONS.get(rca, "Investigate"), "mode": "NOTIFY", "reason": ""}

    if action not in AUTO_ACTIONS:
        d["reason"] = {"K8S_SELF_HEAL": "Kubernetes liveness probe restarts the pod automatically",
                       "K8S_HPA": "HorizontalPodAutoscaler handles edge scaling",
                       "SCHEDULE_MAINTENANCE": "added to the predictive-maintenance schedule"}.get(
            action, "no safe automated action - operator notified")
        d["mode"] = "SCHEDULED" if action == "SCHEDULE_MAINTENANCE" else "NOTIFY"
        if rca == "DEPENDENCY_LATENCY":
            d["mode"], d["reason"] = "MANUAL_APPROVAL", "root cause outside the app - human investigation required"
        return d

    if rca == "MEMORY_LEAK" and (s.get("mem_minutes_to_limit") or 999) > 10:
        d["mode"], d["reason"] = "SCHEDULED", "memory exhaustion predicted in %.0f min - restart scheduled" % s["mem_minutes_to_limit"]
        return d
    if inc["confidence"] < config.AUTO_REMEDIATE_MIN_CONFIDENCE:
        d["mode"], d["reason"] = "MANUAL_APPROVAL", "confidence %.2f below auto-remediation threshold %.2f" % (
            inc["confidence"], config.AUTO_REMEDIATE_MIN_CONFIDENCE)
        return d
    if remediation.circuit_open():
        d["mode"], d["reason"] = "MANUAL_APPROVAL", "circuit breaker open (%d actions in %ds)" % (
            config.MAX_ACTIONS_PER_WINDOW, config.CIRCUIT_WINDOW)
        return d
    if remediation.busy():
        d["mode"], d["reason"] = "BLOCKED", "another remediation is in progress"
        return d
    if remediation.in_cooldown(action):
        d["mode"], d["reason"] = "BLOCKED", "cooldown active for %s" % action
        return d
    d["mode"], d["reason"] = "AUTO", "confidence %.2f >= %.2f and all safety checks passed" % (
        inc["confidence"], config.AUTO_REMEDIATE_MIN_CONFIDENCE)
    return d
