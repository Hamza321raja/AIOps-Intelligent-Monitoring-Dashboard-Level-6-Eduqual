# AIOps Intelligent Infrastructure Monitoring Platform

**EduQual Level 6 - Diploma in Artificial Intelligence Operations**
Intelligent infrastructure monitoring with AIOps, predictive maintenance and automated remediation for a hybrid cloud environment.

## Architecture (8 layers)

| Layer | Implementation |
|---|---|
| 1. Hybrid data sources | On-prem business app (Docker), edge app on Kubernetes (k3s), AWS EC2 host (node-exporter), network probes (blackbox-exporter) |
| 2. Collection & ingestion | Prometheus (metrics), Promtail -> Loki (logs), OpenTelemetry Collector -> Jaeger (traces) |
| 3. Processing & normalisation | OTel processors (memory_limiter, resource enrichment, batch), Promtail JSON parsing + labels, Prometheus recording rules (SLIs) |
| 4. Storage & correlation | Prometheus TSDB, Loki, Jaeger; the engine correlates metrics + logs + health + alerts per incident |
| 5. AIOps intelligence | Isolation Forest + robust z-score (MAD) ensemble, KMeans workload patterns, TensorFlow LSTM CPU forecast, linear capacity forecast, RCA with confidence and blast radius, MLflow tracking |
| 6. Decision & automation | Decision controller (auto / human approval / notify / scheduled), Ansible runbooks, Airflow workflows, Kubernetes probes + HPA, rollback and P1 escalation |
| 7. Visualisation & APM | Grafana dashboard (provisioned), OpenTelemetry APM, AIOps web console (port 8080) |
| 8. SLA & compliance | SLO 99% availability / p95 <= 1s, error budget, SLA reports (API + Airflow schedule), 7 compliance controls, config drift detection, JSON audit trail |

## Services and ports

| Service | Port | Purpose |
|---|---|---|
| AIOps console (nginx) | 8080 | Load generator, fault injection, incidents, approvals, SLA report, audit |
| Grafana | 3001 | Main dashboard "AIOps Intelligent Monitoring Platform" (admin / admin, anonymous view enabled) |
| Prometheus | 9090 | Metrics + 27 alert/recording rules (P1-P4) |
| Alertmanager | 9093 | Grouping, de-duplication, inhibition, priority routing -> engine webhook |
| Jaeger | 16686 | Distributed traces (`aiops-app`, span `db-query`) |
| AIOps engine API | 8000 | `/status /incidents /audit /report /report.md /metrics`, `POST /approve?incident=` |
| MLflow | 5000 | Model training runs (experiment `aiops-engine`) |
| Airflow | 8085 | DAGs `aiops_remediation`, `sla_compliance_report` (admin / admin) |
| Rundeck | 4440 | Manual runbook console (admin / admin) |
| App | 3000 | Business service with `/health`, `/load`, `/chaos/*` |
| Edge app (k3s) | 30007 | NodePort, `/health`, `/break`, `/spike`, `/metrics` |

## Deploy (Ubuntu VM, 8 GB RAM + 4 GB swap recommended)

```bash
git clone https://github.com/Hamza321raja/AIOps-Intelligent-Monitoring-Dashboard-Level-6-Eduqual.git
cd AIOps-Intelligent-Monitoring-Dashboard-Level-6-Eduqual
mkdir -p logs && chmod 777 logs
docker compose up -d --build
docker compose ps

# edge layer (k3s)
curl -sfL https://get.k3s.io | sh -
cd kubernetes/edge && docker build -t edge-app:v4 . && docker save edge-app:v4 | sudo k3s ctr images import - && cd ../..
sudo k3s kubectl apply -f kubernetes/k8s-app.yaml
```

Optional `.env` next to `docker-compose.yml`: `GRAFANA_ADMIN_PASSWORD`, `AIRFLOW_ADMIN_PASSWORD`, `NOTIFY_WEBHOOK_URL` (Slack/Teams incoming webhook).

The ML models need about 5 minutes of traffic before they are trained: start "Low" load in the console right after deploying.

## Automated remediation safety mechanisms

- Confidence gate: only root causes with confidence >= 0.70 are fixed automatically, the rest need human approval
- One action at a time, 60s cooldown per action, circuit breaker (max 5 actions / 10 min)
- Snapshot before, validation after (health checks + CPU), automatic rollback, retry once, then P1 escalation
- Every step is written to `logs/audit.log` (shipped to Loki, shown in Grafana and the console)

See **DEMO_GUIDE.md** for the step-by-step demonstration.
