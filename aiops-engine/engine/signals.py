"""Collects correlated signals from metrics (Prometheus), logs (Loki),
health checks and container configuration for one engine cycle."""
import time

from config import APP_JOB, APP_CONTAINER, SCALE_LEVELS
from engine import prom, docker_ops

A = 'job="%s"' % APP_JOB
_limits_cache = {"t": 0, "cpus": None, "mem": None}


def app_limits():
    """Current CPU/memory limits of the app container (cached for 10s)."""
    if time.time() - _limits_cache["t"] > 10:
        cpus, mem = docker_ops.resources(APP_CONTAINER)
        _limits_cache.update(t=time.time(), cpus=cpus, mem=mem)
    cpus = _limits_cache["cpus"] or SCALE_LEVELS[0][0]
    mem = _limits_cache["mem"] or docker_ops.to_bytes(SCALE_LEVELS[0][1])
    return cpus, mem


def invalidate_limits():
    _limits_cache["t"] = 0


def collect():
    q = prom.query
    cpus, mem_limit = app_limits()

    app_up = q('max(up{%s})' % A, 0.0)
    health = prom.health_check()

    cpu_cores = q('sum(rate(process_cpu_seconds_total{%s}[30s]))' % A, 0.0)
    rps = q('sum(rate(http_requests_total{%s}[30s]))' % A, 0.0)
    err_rps = q('sum(rate(http_requests_total{%s,status=~"5.."}[30s]))' % A, 0.0)
    lat_sum = q('sum(rate(http_request_duration_seconds_sum{%s}[30s]))' % A, 0.0)
    lat_cnt = q('sum(rate(http_request_duration_seconds_count{%s}[30s]))' % A, 0.0)
    p95 = q('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{%s}[1m])))' % A, 0.0)
    mem_bytes = q('max(process_resident_memory_bytes{%s})' % A, 0.0)
    mem_pred_30m = q('max(predict_linear(process_resident_memory_bytes{%s}[5m], 1800))' % A, None)
    mem_slope = q('max(deriv(process_resident_memory_bytes{%s}[5m]))' % A, 0.0)

    # SLA indicators (1h window) computed directly from raw counters
    avail_1h = q('100 * sum(rate(http_requests_total{%s,status!~"5.."}[1h])) / sum(rate(http_requests_total{%s}[1h]))' % (A, A), None)
    avail_5m = q('100 * sum(rate(http_requests_total{%s,status!~"5.."}[5m])) / sum(rate(http_requests_total{%s}[5m]))' % (A, A), None)
    p95_1h = q('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{%s}[1h])))' % A, None)

    # Log-based signal (Loki): application error log lines in the last minute
    log_errors = prom.loki_count('sum(count_over_time({job="aiops", filename=~".*app.log"} |= "application_error" [1m]))', 0.0)

    # Edge (Kubernetes / k3s)
    edge_up = q('max(up{job="kubernetes-edge"})', 0.0)
    edge_cpu = q('max(edge_cpu_usage)', None)

    # Cloud host (AWS EC2 via node-exporter)
    host_cpu = q('100 - 100 * avg(rate(node_cpu_seconds_total{mode="idle"}[1m]))', None)
    host_mem_avail = q('100 * sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)', None)
    disk_free = q('100 * min(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}) / min(node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"})', None)
    disk_avail = q('min(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"})', None)
    disk_slope = q('min(deriv(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}[1h]))', None)
    disk_io_util = q('100 * max(rate(node_disk_io_time_seconds_total[1m]))', None)
    disk_latency_ms = q('1000 * sum(rate(node_disk_read_time_seconds_total[1m]) + rate(node_disk_write_time_seconds_total[1m])) / clamp_min(sum(rate(node_disk_reads_completed_total[1m]) + rate(node_disk_writes_completed_total[1m])), 1)', None)
    swap_used_pct = q('100 * (1 - sum(node_memory_SwapFree_bytes) / clamp_min(sum(node_memory_SwapTotal_bytes), 1))', None)
    net_errors = q('sum(rate(node_network_receive_errs_total[1m]) + rate(node_network_transmit_errs_total[1m]))', 0.0)

    # Hybrid network monitoring (blackbox probes per layer)
    probes = {}
    for labels, v in prom.query_vector('probe_success'):
        probes[labels.get("layer", labels.get("instance", "?"))] = v
    probe_latency = {}
    for labels, v in prom.query_vector('probe_duration_seconds'):
        probe_latency[labels.get("layer", labels.get("instance", "?"))] = round(v, 3)

    error_pct = (err_rps / rps * 100.0) if rps > 0 else 0.0
    avg_latency = (lat_sum / lat_cnt) if lat_cnt > 0 else 0.0
    cpu_util = (cpu_cores / cpus * 100.0) if cpus else 0.0

    mem_minutes_to_limit = None
    if mem_slope and mem_slope > 0 and mem_bytes:
        mem_minutes_to_limit = max(0.0, (mem_limit - mem_bytes) / mem_slope / 60.0)

    disk_hours_to_full = None
    if disk_avail and disk_slope is not None and disk_slope < 0:
        disk_hours_to_full = disk_avail / (-disk_slope) / 3600.0   # linear forecast

    return {
        "ts": time.time(),
        "app_up": app_up,
        "health_code": health,
        "cpu_util": round(cpu_util, 2),
        "cpu_limit": cpus,
        "rps": round(rps, 3),
        "error_pct": round(error_pct, 2),
        "avg_latency": round(avg_latency, 4),
        "p95_latency": round(p95, 4),
        "log_errors_1m": log_errors,
        "mem_mb": round(mem_bytes / 1048576.0, 1),
        "mem_limit_mb": round(mem_limit / 1048576.0, 1),
        "mem_pred_30m_mb": round(mem_pred_30m / 1048576.0, 1) if mem_pred_30m else None,
        "mem_minutes_to_limit": round(mem_minutes_to_limit, 1) if mem_minutes_to_limit is not None else None,
        "availability_1h": round(avail_1h, 3) if avail_1h is not None else None,
        "availability_5m": round(avail_5m, 3) if avail_5m is not None else None,
        "p95_1h": round(p95_1h, 4) if p95_1h is not None else None,
        "edge_up": edge_up,
        "edge_cpu": edge_cpu,
        "host_cpu": round(host_cpu, 2) if host_cpu is not None else None,
        "host_mem_avail_pct": round(host_mem_avail, 2) if host_mem_avail is not None else None,
        "disk_free_pct": round(disk_free, 2) if disk_free is not None else None,
        "disk_hours_to_full": round(disk_hours_to_full, 1) if disk_hours_to_full is not None else None,
        "disk_io_util": round(disk_io_util, 2) if disk_io_util is not None else None,
        "disk_latency_ms": round(disk_latency_ms, 2) if disk_latency_ms is not None else None,
        "swap_used_pct": round(swap_used_pct, 2) if swap_used_pct is not None else None,
        "net_errors": net_errors,
        "probes": probes,
        "probe_latency": probe_latency,
    }


def feature_vector(s):
    """The 5 raw features used by the ML models."""
    return [s["cpu_util"], s["avg_latency"], s["rps"], s["error_pct"], s["p95_latency"]]


FEATURE_NAMES = ["cpu_util", "avg_latency", "rps", "error_pct", "p95_latency"]
