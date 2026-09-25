# Demo guide (15-20 min presentation + live demo)

Open four browser tabs before the exam: **Console** `http://<IP>:8080`, **Grafana** `http://<IP>:3001`, **Jaeger** `:16686`, **MLflow** `:5000` (Airflow `:8085` and Alertmanager `:9093` as needed).

Preparation (10 minutes before): `docker compose ps` (all Up), start **Low (10 rps)** load in the console so the models train (panel 3 shows "Models trained: yes"). Press **Reset all** between scenarios and wait until incidents show RESOLVED.

## Scenario 1 - Application failure -> auto-restart (slide "Scenario 1")
1. Console: **App failure**.
2. Show: backend "Unhealthy (503)", incident `APPLICATION_ERROR` opens (P2, or P1 when the SLA is breached) with evidence (error rate, health check, Loki log correlation) and blast radius (frontend, users).
3. Decision `RESTART (AUTO)`, confidence >= 0.70 -> Ansible `restart_app.yml` -> health validated -> RECOVERING -> RESOLVED with time-to-resolve.
4. Grafana: availability dip, remediation action, audit trail logs. Airflow: `aiops_remediation` DAG run.

## Scenario 2 - Failed remediation -> rollback -> P1 escalation (slide "Scenario 2")
- Option A: **CPU extreme** -> `CPU_BOTTLENECK` -> `SCALE_UP` to 1 CPU -> validation after 35s still > 85% -> **rollback** to 0.5 CPU -> incident **ESCALATED P1**.
- Option B: **Persistent failure** -> restart, retry, still unhealthy -> ESCALATED P1.
Then press **Reset all** (the human fix) and the incident resolves.

## Scenario 3 - Capacity: scale up and right-size (objective b / e)
**CPU saturation** (runs 3 min) -> scale-up succeeds (CPU drops below 85% of the new limit). After the burn ends and CPU stays < 30% for 1 minute, `OVERPROVISIONED` -> `SCALE_DOWN` back to baseline (cost optimisation).

## Scenario 4 - Human-in-the-loop (decision controller)
**Latency +1.5s** with CPU and errors normal -> `DEPENDENCY_LATENCY`, confidence 0.55 -> **MANUAL_APPROVAL**, no automatic action; show the trace in Jaeger (`db-query` span is slow) as the investigation step.

## Scenario 5 - Predictive maintenance
**Memory leak** -> memory grows ~0.5 MB/s -> after 1-2 minutes the linear forecast (`deriv` / `predict_linear`) shows the 512 MB limit being reached in ~15 min -> `MEMORY_LEAK` incident with decision **SCHEDULED** and a maintenance window in panel 6; once fewer than 10 minutes remain the engine performs a `PREEMPTIVE_RESTART` automatically (failure prevented before it happens).

## Scenario 6 - Compliance / configuration drift
On the VM: `docker update --cpus 2 --memory 1g --memory-swap 2g app` (an unapproved change). Within a minute control CTRL-07 fails -> `CONFIG_DRIFT` incident -> `REVERT_CONFIG` restores the desired state; audit shows COMPLIANCE_VIOLATION then COMPLIANCE_RESTORED.

## Scenario 7 - Edge self-healing (Kubernetes)
`curl http://localhost:30007/break` -> liveness probe fails -> Kubernetes restarts the pod (`sudo k3s kubectl get pods` RESTARTS +1). Rollback: `sudo k3s kubectl rollout undo deployment/edge-app`.

## SLA report
Console **Generate SLA report** (or `http://<IP>:8000/report.md`). The Airflow DAG `sla_compliance_report` generates it every 15 minutes into `logs/reports/`.

## Useful commands
```bash
docker compose logs -f aiops-engine        # engine decisions
tail -f logs/audit.log                     # audit trail
curl -s localhost:8000/status | python3 -m json.tool | head -50
docker stats --no-stream                   # memory usage
```
