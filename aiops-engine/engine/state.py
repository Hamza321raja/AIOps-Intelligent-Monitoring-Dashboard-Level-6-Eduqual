"""Shared, thread-safe engine state used by the main loop, remediation
workers and the HTTP API."""
import threading

lock = threading.RLock()

snapshot = {
    "signals": {},
    "ml": {},
    "capacity": {},
    "maintenance": [],
    "compliance": {"score": 100.0, "violations": [], "checked_at": None},
    "decision": {},
    "sla": {},
    "started_at": None,
}

# Prometheus alerts received from Alertmanager, keyed by fingerprint
alerts = {}

# Desired configuration of the app container (what the engine last applied)
desired = {"level": 0}


def update(**kw):
    with lock:
        snapshot.update(kw)


def get():
    with lock:
        return dict(snapshot)
