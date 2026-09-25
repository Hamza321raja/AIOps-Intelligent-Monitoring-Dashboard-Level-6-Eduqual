"""Automated compliance validation and configuration drift detection.

Controls are evaluated against the running containers every minute; results
feed the SLA/compliance report, Prometheus metrics and the audit trail."""
import time

import config
from engine import docker_ops, metrics, remediation, state
from engine.audit import audit, now_iso

CONTROLS = {
    "CTRL-01": "Service running",
    "CTRL-02": "Restart policy set (self-healing on crash)",
    "CTRL-03": "Memory limit set (resource governance)",
    "CTRL-04": "Pinned image version (no ':latest')",
    "CTRL-05": "No privileged containers",
    "CTRL-06": "Docker socket only mounted into aiops-engine",
    "CTRL-07": "App configuration matches desired state (drift detection)",
}
_last = {"t": 0, "violations": set()}


def check(force=False):
    if not force and time.time() - _last["t"] < 60:
        return state.snapshot.get("compliance")
    _last["t"] = time.time()
    info = docker_ops.inspect(config.COMPLIANCE_CONTAINERS)
    results, drift = [], None

    def res(ctrl, target, ok, detail=""):
        results.append({"control": ctrl, "title": CONTROLS[ctrl], "target": target, "passed": bool(ok), "detail": detail})

    for name in config.COMPLIANCE_CONTAINERS:
        c = info.get(name)
        if not c:
            res("CTRL-01", name, False, "container not found")
            continue
        hc = c.get("HostConfig", {})
        res("CTRL-01", name, c.get("State", {}).get("Running"), c.get("State", {}).get("Status", ""))
        res("CTRL-02", name, hc.get("RestartPolicy", {}).get("Name") in ("always", "unless-stopped"),
            hc.get("RestartPolicy", {}).get("Name", ""))
        res("CTRL-03", name, (hc.get("Memory") or 0) > 0, "%s MB" % round((hc.get("Memory") or 0) / 1048576.0))
        image = c.get("Config", {}).get("Image", "")
        res("CTRL-04", name, ":" in image.split("/")[-1] and not image.endswith(":latest"), image)
        res("CTRL-05", name, not hc.get("Privileged"), "privileged" if hc.get("Privileged") else "")
        socket = any("docker.sock" in (m.get("Source") or "") for m in c.get("Mounts", []))
        res("CTRL-06", name, (not socket) or name == "aiops-engine", "docker.sock mounted" if socket else "")

    app = info.get(config.APP_CONTAINER)
    if app and not remediation.busy():
        hc = app.get("HostConfig", {})
        cpus = (hc.get("NanoCpus") or 0) / 1e9
        mem = hc.get("Memory") or 0
        lvl = state.desired.get("level", 0)
        want_cpus, want_mem, _ = config.SCALE_LEVELS[lvl]
        ok = abs(cpus - want_cpus) < 0.01 and mem == docker_ops.to_bytes(want_mem)
        detail = "running cpus=%s mem=%sMB, desired cpus=%s mem=%s" % (cpus, round(mem / 1048576.0), want_cpus, want_mem)
        res("CTRL-07", config.APP_CONTAINER, ok, detail)
        if not ok:
            drift = detail

    failed = [r for r in results if not r["passed"]]
    score = round(100.0 * (len(results) - len(failed)) / len(results), 1) if results else 100.0
    keys = {(r["control"], r["target"]) for r in failed}
    for k in keys - _last["violations"]:
        audit("COMPLIANCE_VIOLATION", control=k[0], target=k[1], title=CONTROLS[k[0]])
    for k in _last["violations"] - keys:
        audit("COMPLIANCE_RESTORED", control=k[0], target=k[1], title=CONTROLS[k[0]])
    _last["violations"] = keys

    metrics.COMPLIANCE_VIOL.set(len(failed))
    metrics.COMPLIANCE_SCORE.set(score)
    out = {"checked_at": now_iso(), "score": score, "total_checks": len(results),
           "violations": failed, "drift": drift, "results": results}
    state.update(compliance=out)
    return out


def drift_hypothesis(comp):
    """Configuration drift becomes an incident handled by the normal pipeline."""
    if comp and comp.get("drift"):
        conf = 0.9 if config.ENFORCE_COMPLIANCE else 0.6
        return {"rca": "CONFIG_DRIFT", "confidence": conf, "service": "app", "blast_radius": ["frontend", "users"],
                "evidence": ["unapproved change: " + comp["drift"]]}
    return None
