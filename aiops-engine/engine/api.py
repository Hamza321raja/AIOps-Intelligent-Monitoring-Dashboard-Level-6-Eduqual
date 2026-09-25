"""HTTP API of the AIOps engine (port 8000).

GET  /metrics            Prometheus metrics
GET  /health             liveness
GET  /status             live engine state (used by the frontend dashboard)
GET  /incidents          incident list
GET  /audit              recent audit trail entries
GET  /report[?window=1h] generate an SLA compliance report (JSON)
GET  /report.md          same report as Markdown
POST /alerts             Alertmanager webhook receiver
POST /audit              external audit events (e.g. from Airflow)
POST /approve?incident=INC-0001   human approval of a recommended action
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import config
from engine import alerting, audit as audit_mod, decision, remediation, report, state

logger = logging.getLogger("aiops.api")


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _public(inc):
    keys = ("id", "rca", "service", "priority", "confidence", "status", "opened_at", "resolved_at", "ttr_s",
            "occurrences", "evidence", "secondary", "blast_radius", "alerts", "decision", "escalation_reason",
            "reopened")
    out = {k: inc.get(k) for k in keys if k in inc}
    out["actions"] = [{k: a.get(k) for k in ("action", "actor", "result", "duration_s", "rollback", "before", "after", "steps")}
                      for a in inc.get("actions", [])]
    return out


def status_payload():
    snap = state.get()
    with alerting._lock:
        incs = [_public(i) for i in reversed(alerting.incidents[-25:])]
    return {
        "signals": snap.get("signals"), "ml": snap.get("ml"), "capacity": snap.get("capacity"),
        "maintenance": snap.get("maintenance"), "sla": snap.get("sla"), "decision": snap.get("decision"),
        "health_score": snap.get("health_score"),
        "compliance": {k: (snap.get("compliance") or {}).get(k) for k in ("score", "violations", "checked_at", "drift")},
        "incidents": incs, "open_incidents": len(alerting.open_incidents()),
        "stats": dict(alerting.stats, mttr_s=alerting.mttr(), noise_reduction_ratio=alerting.noise_reduction_ratio()),
        "safety": {"circuit_breaker_open": remediation.circuit_open(), "remediation_in_progress": remediation.busy(),
                   "min_confidence": config.AUTO_REMEDIATE_MIN_CONFIDENCE, "dry_run": config.DRY_RUN,
                   "scale_level": state.desired.get("level", 0)},
        "active_alerts": [{"alert": a["labels"].get("alertname"), "priority": a["labels"].get("priority"),
                           "service": a["labels"].get("service")}
                          for a in list(state.alerts.values()) if a.get("status") == "firing"],
        "audit": list(audit_mod.recent)[:15],
        "started_at": snap.get("started_at"),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str).encode()
                                                      if ctype == "application/json" else str(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/metrics":
            return self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
        if u.path == "/health":
            return self._send(200, {"status": "ok"})
        if u.path == "/status":
            return self._send(200, status_payload())
        if u.path == "/incidents":
            with alerting._lock:
                return self._send(200, [_public(i) for i in reversed(alerting.incidents)])
        if u.path == "/audit":
            return self._send(200, list(audit_mod.recent))
        if u.path in ("/report", "/report.md"):
            rep = report.generate(qs.get("window", [config.SLA_WINDOW])[0])
            audit_mod.audit("SLA_REPORT_GENERATED", actor=qs.get("actor", ["api"])[0], window=rep["window"],
                            compliant=rep["compliant"])
            if u.path == "/report.md":
                return self._send(200, report.to_markdown(rep), "text/markdown; charset=utf-8")
            return self._send(200, rep)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/alerts", "/event"):
            n = alerting.ingest_alertmanager(self._body())
            return self._send(200, {"received": n})
        if u.path == "/audit":
            b = self._body()
            audit_mod.audit(b.pop("event", "EXTERNAL_EVENT"), actor=b.pop("actor", "external"), **b)
            return self._send(200, {"ok": True})
        if u.path == "/approve":
            inc = alerting.get((qs.get("incident") or [""])[0])
            if not inc:
                return self._send(404, {"error": "incident not found"})
            d = inc.get("decision") or decision.decide(inc, state.snapshot.get("signals") or {})
            if d["action"] not in decision.AUTO_ACTIONS:
                return self._send(400, {"error": "action %s cannot be automated" % d["action"]})
            audit_mod.audit("HUMAN_APPROVAL", actor="operator", incident=inc["id"], action=d["action"])
            started = remediation.start(inc, d["action"], actor="operator")
            return self._send(200 if started else 409, {"started": started, "action": d["action"]})
        return self._send(404, {"error": "not found"})


def serve():
    srv = _Server(("0.0.0.0", config.API_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info("AIOps API listening on :%d", config.API_PORT)
    return srv
