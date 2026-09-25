"""Append-only audit trail. Every automated or human action is written as one
JSON line to /app/logs/audit.log, which Promtail ships to Loki."""
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone

from config import AUDIT_PATH, LOG_DIR

logger = logging.getLogger("aiops.audit")
_lock = threading.Lock()
recent = deque(maxlen=300)
os.makedirs(LOG_DIR, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(event, actor="aiops-bot", **fields):
    entry = {"type": "audit", "ts": now_iso(), "actor": actor, "event": event}
    entry.update(fields)
    line = json.dumps(entry, default=str)
    with _lock:
        recent.appendleft(entry)
        try:
            with open(AUDIT_PATH, "a") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.error("Audit write failed: %s", e)
    logger.info(line)
    return entry
