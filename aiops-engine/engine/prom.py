"""Small, defensive clients for Prometheus, Loki and the app health endpoint."""
import math
import logging
import requests

from config import PROM_BASE, LOKI_BASE, APP_HEALTH_URL

logger = logging.getLogger("aiops.prom")


def _clean(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def query(q, default=None):
    """Instant PromQL query returning the first sample value (use sum() in q)."""
    try:
        r = requests.get(PROM_BASE + "/api/v1/query", params={"query": q}, timeout=3)
        data = r.json()
        if data.get("status") != "success":
            return default
        res = data["data"]["result"]
        if not res:
            return default
        v = _clean(res[0]["value"][1])
        return default if v is None else v
    except Exception as e:
        logger.debug("Prometheus query failed (%s): %s", q, e)
        return default


def query_vector(q):
    """Instant PromQL query returning [(labels, value), ...]."""
    try:
        r = requests.get(PROM_BASE + "/api/v1/query", params={"query": q}, timeout=3)
        data = r.json()
        out = []
        for item in data.get("data", {}).get("result", []):
            v = _clean(item["value"][1])
            if v is not None:
                out.append((item.get("metric", {}), v))
        return out
    except Exception as e:
        logger.debug("Prometheus vector query failed (%s): %s", q, e)
        return []


def loki_count(q, default=0.0):
    """Instant LogQL metric query (e.g. sum(count_over_time(...)))."""
    try:
        r = requests.get(LOKI_BASE + "/loki/api/v1/query", params={"query": q}, timeout=3)
        res = r.json().get("data", {}).get("result", [])
        if not res:
            return default
        v = _clean(res[0]["value"][1])
        return default if v is None else v
    except Exception as e:
        logger.debug("Loki query failed: %s", e)
        return default


def health_check(url=APP_HEALTH_URL, timeout=2):
    """Returns the HTTP status code of the health endpoint, or None if unreachable."""
    try:
        return requests.get(url, timeout=timeout).status_code
    except Exception:
        return None
