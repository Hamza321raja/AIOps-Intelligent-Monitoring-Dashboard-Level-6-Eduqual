"""Central configuration for the AIOps engine. Every value can be overridden
with an environment variable (see docker-compose.yml)."""
import os


def _env(name, default):
    return os.getenv(name, default)


def _float(name, default):
    return float(_env(name, default))


def _int(name, default):
    return int(_env(name, default))


def _bool(name, default):
    return str(_env(name, default)).lower() in ("1", "true", "yes")


# ===============================
# DATA SOURCES
# ===============================
PROM_BASE = _env("PROM_BASE", "http://prometheus:9090")
PROM_URL = PROM_BASE + "/api/v1/query"          # kept for backwards compatibility
LOKI_BASE = _env("LOKI_BASE", "http://loki:3100")
JAEGER_BASE = _env("JAEGER_BASE", "http://jaeger:16686")
APP_HEALTH_URL = _env("APP_HEALTH_URL", "http://app:3000/health")
APP_JOB = _env("APP_JOB", "onprem-app")
APP_CONTAINER = _env("APP_CONTAINER", "app")

# ===============================
# ENGINE LOOP / API
# ===============================
LOOP_INTERVAL = _int("LOOP_INTERVAL", 5)
API_PORT = _int("API_PORT", 8000)
LOG_DIR = _env("LOG_DIR", "/app/logs")
LOG_PATH = os.path.join(LOG_DIR, "aiops.log")
AUDIT_PATH = os.path.join(LOG_DIR, "audit.log")
REPORT_DIR = os.path.join(LOG_DIR, "reports")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# ===============================
# MACHINE LEARNING
# ===============================
MLFLOW_TRACKING_URI = _env("MLFLOW_TRACKING_URI", "http://mlflow:5000")
MLFLOW_EXPERIMENT = _env("MLFLOW_EXPERIMENT", "aiops-engine")
ANOMALY_CONTAMINATION = _float("ANOMALY_CONTAMINATION", 0.05)
MIN_TRAINING_SAMPLES = _int("MIN_TRAINING_SAMPLES", 60)     # 60 x 5s = 5 minutes warm-up
RETRAIN_EVERY = _int("RETRAIN_EVERY", 60)                   # retrain after 60 new samples
LSTM_WINDOW = _int("LSTM_WINDOW", 12)                       # 12 x 5s = 1 minute of history
LSTM_EPOCHS = _int("LSTM_EPOCHS", 8)
ZSCORE_THRESHOLD = _float("ZSCORE_THRESHOLD", 3.0)          # robust z-score (MAD) threshold
DRIFT_THRESHOLD = _float("DRIFT_THRESHOLD", 3.0)

# ===============================
# SLA / SLO
# ===============================
SLO_AVAILABILITY = _float("SLO_AVAILABILITY", 99.0)   # percent of non-5xx requests
SLO_P95_LATENCY = _float("SLO_P95_LATENCY", 1.0)      # seconds
SLA_WINDOW = _env("SLA_WINDOW", "1h")
SLA = {  # legacy thresholds used by the rule-based checks
    "cpu": _float("SLA_CPU", 85),
    "latency": SLO_P95_LATENCY,
    "error_pct": _float("SLA_ERROR_PCT", 5),
}

# ===============================
# DECISION CONTROLLER / SAFETY
# ===============================
AUTO_REMEDIATE_MIN_CONFIDENCE = _float("AUTO_REMEDIATE_MIN_CONFIDENCE", 0.7)
ACTION_COOLDOWN = _int("ACTION_COOLDOWN", 60)
MAX_ACTIONS_PER_WINDOW = _int("MAX_ACTIONS_PER_WINDOW", 5)
CIRCUIT_WINDOW = _int("CIRCUIT_WINDOW", 600)
VALIDATE_WAIT = _int("VALIDATE_WAIT", 35)
HEALTH_TIMEOUT = _int("HEALTH_TIMEOUT", 45)
DEDUP_WINDOW = _int("DEDUP_WINDOW", 300)        # 5-minute alert/incident dedup window
RESOLVE_AFTER_CYCLES = _int("RESOLVE_AFTER_CYCLES", 3)
DRY_RUN = _bool("DRY_RUN", False)
ENFORCE_COMPLIANCE = _bool("ENFORCE_COMPLIANCE", True)
NOTIFY_WEBHOOK_URL = _env("NOTIFY_WEBHOOK_URL", "")   # optional Slack/Teams incoming webhook

# App resource ladder for vertical scaling: (cpus, memory, memory+swap)
SCALE_LEVELS = [
    (0.5, "512m", "1g"),
    (1.0, "768m", "1536m"),
    (1.5, "1g", "2g"),
]
BASELINE_LEVEL = 0

# ===============================
# AUTOMATION / ORCHESTRATION
# ===============================
ANSIBLE_PATH = _env("ANSIBLE_PATH", "/ansible")
AIRFLOW_URL = _env("AIRFLOW_URL", "http://airflow:8080")
AIRFLOW_USER = _env("AIRFLOW_USER", "admin")
AIRFLOW_PASSWORD = _env("AIRFLOW_PASSWORD", "admin")
RUNDECK_URL = _env("RUNDECK_URL", "http://rundeck:4440")
RUNDECK_TOKEN = _env("RUNDECK_TOKEN", "")
ENABLE_RUNDECK = _bool("ENABLE_RUNDECK", False)
ENABLE_AIRFLOW = _bool("ENABLE_AIRFLOW", True)

# ===============================
# PREDICTIVE MAINTENANCE
# ===============================
MAINTENANCE_HOUR_UTC = _int("MAINTENANCE_HOUR_UTC", 2)
MEMORY_RISK_MINUTES = _int("MEMORY_RISK_MINUTES", 30)
DISK_RISK_HOURS = _int("DISK_RISK_HOURS", 24)

# ===============================
# COMPLIANCE
# ===============================
COMPLIANCE_CONTAINERS = [c for c in _env(
    "COMPLIANCE_CONTAINERS",
    "frontend,app,aiops-engine,otel-collector,jaeger,prometheus,alertmanager,"
    "grafana,loki,promtail,mlflow,airflow,rundeck,node-exporter,blackbox-exporter",
).split(",") if c]

# Service topology used for dependency mapping and blast-radius analysis
# (edges point from a service to what it depends on).
DEPENDENCY_MAP = {
    "users": ["frontend"],
    "frontend": ["app"],
    "app": ["otel-collector"],
    "otel-collector": ["jaeger"],
    "grafana": ["prometheus", "loki", "jaeger"],
    "prometheus": ["app", "node-exporter", "kubernetes-edge", "blackbox-exporter", "aiops-engine"],
    "promtail": ["loki"],
    "aiops-engine": ["prometheus", "loki", "mlflow", "airflow"],
    "app-host": ["ec2-host"],
    "kubernetes-edge": ["ec2-host"],
}
