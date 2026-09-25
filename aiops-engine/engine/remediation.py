"""Automated remediation with safety mechanisms.

Safety: confidence gate (decision.py), per-action cooldown, one action at a
time, circuit breaker (max N actions per window), pre-action snapshot,
post-action health/metric validation, automatic rollback and escalation.
"""
import logging
import threading
import time
from collections import deque

import config
from engine import alerting, docker_ops, metrics, orchestration, prom, signals, state
from engine.audit import audit, now_iso
from engine.rca import learn_incident
from engine.runbook import runbook

logger = logging.getLogger("aiops.remediation")

_busy = threading.Lock()
_action_times = deque()
_last_action = {}


def busy():
    return _busy.locked()


def circuit_open():
    now = time.time()
    while _action_times and now - _action_times[0] > config.CIRCUIT_WINDOW:
        _action_times.popleft()
    is_open = len(_action_times) >= config.MAX_ACTIONS_PER_WINDOW
    metrics.CIRCUIT.set(1 if is_open else 0)
    return is_open


def in_cooldown(action):
    return time.time() - _last_action.get(action, 0) < config.ACTION_COOLDOWN


def verify_health(timeout=None):
    """Healthy = two consecutive HTTP 200 responses within the timeout."""
    deadline = time.time() + (timeout or config.HEALTH_TIMEOUT)
    ok_count = 0
    while time.time() < deadline:
        ok_count = ok_count + 1 if prom.health_check() == 200 else 0
        if ok_count >= 2:
            return True
        time.sleep(2)
    return False


def apply_level(level):
    cpus, mem, swap = config.SCALE_LEVELS[level]
    ok, out = docker_ops.run_playbook("scale_app.yml", {"container": config.APP_CONTAINER, "cpus": cpus,
                                                          "memory": mem, "memory_swap": swap})
    if ok:
        state.desired["level"] = level
        metrics.SCALE_LEVEL.set(level)
    signals.invalidate_limits()
    return ok


def current_cpu_util():
    cpus, _ = signals.app_limits()
    cores = prom.query('sum(rate(process_cpu_seconds_total{job="%s"}[30s]))' % config.APP_JOB, 0.0)
    return cores / cpus * 100.0 if cpus else 0.0


def start(inc, action, actor="aiops-bot"):
    """Runs the action in a background worker. Returns False if not started."""
    if not _busy.acquire(blocking=False):
        return False
    _action_times.append(time.time())
    _last_action[action] = time.time()
    circuit_open()
    alerting.mark(inc, "REMEDIATING")
    threading.Thread(target=_execute, args=(inc, action, actor), daemon=True).start()
    return True


def _execute(inc, action, actor):
    t0 = time.time()
    steps = []
    level = state.desired.get("level", 0)
    before_cpus, before_mem = docker_ops.resources(config.APP_CONTAINER)
    before = {"cpus": before_cpus, "memory_mb": round(before_mem / 1048576.0) if before_mem else None, "level": level}
    audit("REMEDIATION_STARTED", actor=actor, incident=inc["id"], action=action, rca=inc["rca"],
          confidence=inc["confidence"], runbook=runbook(action), before=before)
    result, rollback, escalate_reason = "FAILED", None, None

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": bool(ok), "detail": detail, "at": now_iso()})
        return ok

    try:
        if action in ("RESTART", "PREEMPTIVE_RESTART"):
            step("snapshot_state", True, str(before))
            ok, _ = docker_ops.run_playbook("restart_app.yml", {"container_name": config.APP_CONTAINER})
            step("restart_container", ok)
            healthy = ok and step("validate_health", verify_health())
            if not healthy:
                ok, _ = docker_ops.run_playbook("restart_app.yml", {"container_name": config.APP_CONTAINER})
                step("retry_restart", ok)
                healthy = ok and step("validate_health_retry", verify_health())
            if healthy:
                result = "SUCCESS"
            else:
                rollback = "No configuration was changed, nothing to roll back; service still unhealthy after retry"
                escalate_reason = "restart did not restore health (2 attempts)"

        elif action == "SCALE_UP":
            if level >= len(config.SCALE_LEVELS) - 1:
                step("check_scale_ceiling", False, "already at max level %d" % level)
                escalate_reason = "scale ceiling reached - capacity must be added manually"
                result = "BLOCKED"
            else:
                step("check_scale_ceiling", True, "level %d -> %d" % (level, level + 1))
                ok = step("apply_limits", apply_level(level + 1), str(config.SCALE_LEVELS[level + 1][:2]))
                time.sleep(config.VALIDATE_WAIT)
                util = current_cpu_util()
                healthy = prom.health_check() == 200
                improved = ok and healthy and util < config.SLA["cpu"]
                step("validate_health_and_cpu", improved, "health=%s cpu_util=%.0f%%" % (healthy, util))
                if improved:
                    result = "SUCCESS"
                else:
                    rb = apply_level(level)
                    rollback = "restored level %d %s (%s)" % (level, config.SCALE_LEVELS[level][:2], "ok" if rb else "FAILED")
                    step("rollback", rb, rollback)
                    escalate_reason = "scale-up did not relieve CPU (util %.0f%%)" % util

        elif action in ("SCALE_DOWN", "REVERT_CONFIG"):
            target = config.BASELINE_LEVEL if action == "SCALE_DOWN" else state.desired.get("level", 0)
            ok = step("apply_limits", apply_level(target), str(config.SCALE_LEVELS[target][:2]))
            time.sleep(min(config.VALIDATE_WAIT, 20))
            util = current_cpu_util()
            healthy = prom.health_check() == 200
            good = ok and healthy and util < config.SLA["cpu"]
            step("validate_health_and_cpu", good, "health=%s cpu_util=%.0f%%" % (healthy, util))
            if good:
                result = "SUCCESS"
            elif action == "SCALE_DOWN":
                rb = apply_level(level)
                rollback = "restored level %d (%s)" % (level, "ok" if rb else "FAILED")
                step("rollback", rb, rollback)
                escalate_reason = "right-sizing degraded the service"
        else:
            result = "UNSUPPORTED"
    except Exception as e:
        logger.exception("Remediation crashed")
        step("exception", False, str(e))
        escalate_reason = "remediation error: %s" % e
    finally:
        after_cpus, after_mem = docker_ops.resources(config.APP_CONTAINER)
        after = {"cpus": after_cpus, "memory_mb": round(after_mem / 1048576.0) if after_mem else None,
                 "level": state.desired.get("level", 0)}
        record = {"action": action, "actor": actor, "result": result, "started_at": now_iso(),
                  "duration_s": round(time.time() - t0, 1), "steps": steps, "before": before, "after": after,
                  "rollback": rollback}
        inc["actions"].append(record)
        metrics.REMEDIATIONS.labels(action, result).inc()
        audit("REMEDIATION_FINISHED", actor=actor, incident=inc["id"], action=action, result=result,
              duration_s=record["duration_s"], steps=steps, before=before, after=after, rollback=rollback)
        if result == "SUCCESS":
            alerting.stats["auto_remediated"] += 1
            alerting.mark(inc, "RECOVERING")
            learn_incident([0, 0, 0, 0, 0] if not state.snapshot.get("signals") else
                           signals.feature_vector(state.snapshot["signals"]), inc["rca"])
        else:
            if rollback:
                audit("ROLLBACK_EXECUTED", incident=inc["id"], action=action, detail=rollback)
            metrics.ESCALATIONS.labels("remediation_failed").inc()
            alerting.escalate(inc, escalate_reason or "remediation %s" % result, priority="P1")
        orchestration.orchestrate(action, inc, result)
        _busy.release()


# backwards compatibility
def remediate(action):
    return "DEPRECATED"
