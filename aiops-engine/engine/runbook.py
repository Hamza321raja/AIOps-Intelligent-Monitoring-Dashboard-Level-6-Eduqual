"""Runbook definitions: the ordered steps executed for each automated action."""
RUNBOOKS = {
    "RESTART": ["snapshot_state", "inspect_service", "restart_container (ansible: restart_app.yml)",
                "validate_health", "retry_once_if_unhealthy", "escalate_if_still_unhealthy", "record_audit"],
    "PREEMPTIVE_RESTART": ["snapshot_state", "confirm_memory_forecast", "restart_container (ansible: restart_app.yml)",
                           "validate_health", "record_audit"],
    "SCALE_UP": ["snapshot_state", "check_scale_ceiling", "apply_limits (ansible: scale_app.yml)",
                 "wait_for_metrics", "validate_health_and_cpu", "rollback_if_not_improved", "record_audit"],
    "SCALE_DOWN": ["snapshot_state", "apply_baseline_limits (ansible: scale_app.yml)", "validate_health_and_cpu",
                   "rollback_if_degraded", "record_audit"],
    "REVERT_CONFIG": ["detect_drift", "apply_desired_limits (ansible: scale_app.yml)", "validate_health", "record_audit"],
}

RECOMMENDATIONS = {
    "SERVICE_DOWN": "Restart the service; if it keeps failing inspect recent deployments and container logs.",
    "APPLICATION_ERROR": "Restart to clear faulty state; review application_error logs in Loki and recent changes.",
    "CPU_BOTTLENECK": "Increase the CPU limit (vertical scaling) or add replicas; profile hot code paths.",
    "PREDICTED_CPU_SATURATION": "Scale up before saturation; tune the auto-scaling threshold if this repeats.",
    "MEMORY_LEAK": "Restart pre-emptively in the maintenance window; investigate heap growth.",
    "OVERPROVISIONED": "Right-size to the baseline limits to reduce cost.",
    "DEPENDENCY_LATENCY": "Check the slow downstream dependency / I/O in Jaeger; add caching or timeouts.",
    "EDGE_DOWN": "Kubernetes restarts the pod via its liveness probe; roll back with 'kubectl rollout undo' if a new version is faulty.",
    "EDGE_CPU_HIGH": "The HorizontalPodAutoscaler adds edge replicas; raise maxReplicas if saturated.",
    "HOST_MEMORY_PRESSURE": "Resize the EC2 instance or stop non-critical services.",
    "DISK_CAPACITY_RISK": "Clean up logs / docker images or extend the EBS volume before it fills.",
    "NETWORK_ISSUE": "Check security groups, DNS and the failing probe target.",
    "UNKNOWN_ANOMALY": "Investigate the anomalous metrics in Grafana; label the incident to improve RCA.",
    "PERFORMANCE_DRIFT": "Performance moved away from its baseline; compare with recent changes.",
    "CONFIG_DRIFT": "Unapproved configuration change detected; reverting to the desired state.",
}


def runbook(action, rca=None):
    return list(RUNBOOKS.get(action, ["monitor"]))
