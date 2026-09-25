"""Automated SLA compliance reporting (JSON + Markdown). Triggered on demand
through the API and on a schedule by the Airflow DAG 'sla_compliance_report'."""
import json
import os
from datetime import datetime, timezone

import config
from engine import alerting, prom, state

A = 'job="%s"' % config.APP_JOB


def _q(expr, window):
    return prom.query(expr.replace("$W", window).replace("$A", A), None)


def generate(window="1h", write=True):
    avail = _q('100 * sum(rate(http_requests_total{$A,status!~"5.."}[$W])) / sum(rate(http_requests_total{$A}[$W]))', window)
    p95 = _q('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{$A}[$W])))', window)
    rps = _q('sum(rate(http_requests_total{$A}[$W]))', window)
    uptime = _q('100 * avg_over_time(up{$A}[$W])', window)
    edge_up = _q('100 * avg_over_time(up{job="kubernetes-edge"}[$W])', window)
    host_up = _q('100 * avg_over_time(up{job="node-exporter"}[$W])', window)

    budget = None
    if avail is not None:
        allowed = 100.0 - config.SLO_AVAILABILITY
        budget = max(-100.0, round(100.0 * (1 - (100.0 - avail) / allowed), 1)) if allowed > 0 else None
    avail_ok = avail is None or avail >= config.SLO_AVAILABILITY
    lat_ok = p95 is None or p95 <= config.SLO_P95_LATENCY

    incs = list(alerting.incidents)
    by_rca = {}
    for i in incs:
        by_rca[i["rca"]] = by_rca.get(i["rca"], 0) + 1
    comp = state.snapshot.get("compliance") or {}
    rep = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": window,
        "slo": {"availability_target": config.SLO_AVAILABILITY, "p95_latency_target_s": config.SLO_P95_LATENCY},
        "sli": {"availability_pct": None if avail is None else round(avail, 3),
                "p95_latency_s": None if p95 is None else round(p95, 3),
                "avg_rps": None if rps is None else round(rps, 2),
                "uptime_pct": {"on-prem app": uptime, "kubernetes edge": edge_up, "aws ec2 host": host_up}},
        "error_budget_remaining_pct": budget,
        "compliant": bool(avail_ok and lat_ok),
        "breaches": [b for b, bad in (("availability", not avail_ok), ("p95_latency", not lat_ok)) if bad],
        "incidents": {"total": len(incs), "open": len(alerting.open_incidents()),
                      "auto_remediated": alerting.stats["auto_remediated"],
                      "escalated": alerting.stats["escalated"], "mttr_s": alerting.mttr(),
                      "noise_reduction_ratio": alerting.noise_reduction_ratio(), "by_root_cause": by_rca},
        "compliance": {"score_pct": comp.get("score"), "violations": [
            "%s %s: %s" % (v["control"], v["target"], v["title"]) for v in comp.get("violations", [])]},
    }
    if write:
        os.makedirs(config.REPORT_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base = os.path.join(config.REPORT_DIR, "sla_report_%s" % stamp)
        with open(base + ".json", "w") as f:
            json.dump(rep, f, indent=2, default=str)
        with open(base + ".md", "w") as f:
            f.write(to_markdown(rep))
        rep["files"] = [base + ".json", base + ".md"]
    return rep


def _fmt(v, suffix=""):
    return "n/a" if v is None else "%s%s" % (round(v, 3) if isinstance(v, float) else v, suffix)


def to_markdown(r):
    s, i = r["sli"], r["incidents"]
    lines = [
        "# SLA Compliance Report", "",
        "Generated: %s  |  Window: %s  |  Status: **%s**" % (r["generated_at"], r["window"],
                                                           "COMPLIANT" if r["compliant"] else "BREACHED"), "",
        "| Indicator | Target | Actual | Result |", "|---|---|---|---|",
        "| Availability | %s%% | %s | %s |" % (r["slo"]["availability_target"], _fmt(s["availability_pct"], "%"),
                                           "FAIL" if "availability" in r["breaches"] else "PASS"),
        "| p95 latency | %ss | %s | %s |" % (r["slo"]["p95_latency_target_s"], _fmt(s["p95_latency_s"], "s"),
                                          "FAIL" if "p95_latency" in r["breaches"] else "PASS"),
        "| Error budget remaining | > 0%% | %s | %s |" % (_fmt(r["error_budget_remaining_pct"], "%"),
                                                     "PASS" if (r["error_budget_remaining_pct"] or 0) > 0 else "FAIL"),
        "", "## Hybrid uptime", "",
    ]
    for k, v in s["uptime_pct"].items():
        lines.append("- %s: %s" % (k, _fmt(v, "%")))
    lines += ["", "## Incidents", "",
              "- Incidents: %s (open %s) | successful automated actions: %s | escalations: %s" % (i["total"], i["open"], i["auto_remediated"], i["escalated"]),
              "- MTTR: %ss, alert noise reduction: %.0f%%" % (i["mttr_s"], 100 * i["noise_reduction_ratio"]),
              "- By root cause: %s" % (", ".join("%s=%s" % kv for kv in i["by_root_cause"].items()) or "none"),
              "", "## Compliance controls", "", "- Score: %s%%" % _fmt(r["compliance"]["score_pct"])]
    lines += ["- VIOLATION %s" % v for v in r["compliance"]["violations"]] or ["- No violations"]
    return "\n".join(lines) + "\n"
