"""Thin wrappers around the Docker CLI and ansible-playbook (via the mounted socket)."""
import json
import logging
import os
import subprocess

from config import ANSIBLE_PATH, DRY_RUN

logger = logging.getLogger("aiops.docker")


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def inspect(names):
    """docker inspect for several containers; missing ones are simply absent."""
    if isinstance(names, str):
        names = [names]
    code, out, err = _run(["docker", "inspect"] + list(names), timeout=20)
    try:
        return {c["Name"].lstrip("/"): c for c in json.loads(out or "[]")}
    except Exception:
        logger.debug("docker inspect failed: %s", err)
        return {}


def resources(container):
    """Current (cpus, memory_bytes) limits of a container, or (None, None)."""
    info = inspect(container).get(container)
    if not info:
        return None, None
    hc = info.get("HostConfig", {})
    nano = hc.get("NanoCpus") or 0
    cpus = nano / 1e9 if nano else None
    mem = hc.get("Memory") or None
    return cpus, mem


def run_playbook(name, extra_vars=None, timeout=120):
    """Runs an Ansible playbook from /ansible. Returns (ok, output)."""
    path = os.path.join(ANSIBLE_PATH, name)
    cmd = ["ansible-playbook", path]
    for k, v in (extra_vars or {}).items():
        cmd += ["-e", "%s=%s" % (k, v)]
    if DRY_RUN:
        logger.info("[DRY_RUN] %s", " ".join(cmd))
        return True, "dry-run"
    code, out, err = _run(cmd, timeout=timeout)
    ok = code == 0 and "failed=0" in out and "unreachable=0" in out
    if not ok:
        logger.error("Playbook %s failed: %s %s", name, out[-500:], err[-500:])
    return ok, (out + err)[-2000:]


def to_bytes(mem):
    """'512m' -> bytes."""
    s = str(mem).lower().strip()
    mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s))
