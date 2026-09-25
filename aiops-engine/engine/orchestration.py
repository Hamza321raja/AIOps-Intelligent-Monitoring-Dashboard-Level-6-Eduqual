"""Workflow orchestration: triggers Apache Airflow DAGs (and optionally a
Rundeck job) after automated actions. Runs in background threads so the
engine loop is never blocked."""
import logging
import threading
import time

import requests

import config
from engine.audit import audit

logger = logging.getLogger("aiops.orchestration")


def _post(url, payload, auth=None, headers=None):
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, auth=auth, headers=headers, timeout=5)
            if r.status_code in (200, 201):
                return True, r.json() if r.content else {}
            logger.warning("HTTP %s from %s: %s", r.status_code, url, r.text[:200])
        except Exception as e:
            logger.warning("Orchestration request failed: %s", e)
        time.sleep(1 + attempt)
    return False, None


def trigger_airflow(dag_id, conf):
    if not config.ENABLE_AIRFLOW:
        return

    def _run():
        ok, res = _post("%s/api/v1/dags/%s/dagRuns" % (config.AIRFLOW_URL, dag_id), {"conf": conf},
                        auth=(config.AIRFLOW_USER, config.AIRFLOW_PASSWORD))
        audit("WORKFLOW_TRIGGERED" if ok else "WORKFLOW_TRIGGER_FAILED", orchestrator="airflow", dag=dag_id,
              incident=conf.get("incident_id"), dag_run=(res or {}).get("dag_run_id"))
    threading.Thread(target=_run, daemon=True).start()


def trigger_rundeck(job_id, options):
    if not (config.ENABLE_RUNDECK and config.RUNDECK_TOKEN):
        return

    def _run():
        ok, res = _post("%s/api/41/job/%s/run" % (config.RUNDECK_URL, job_id), {"options": options},
                        headers={"X-Rundeck-Auth-Token": config.RUNDECK_TOKEN, "Accept": "application/json"})
        audit("WORKFLOW_TRIGGERED" if ok else "WORKFLOW_TRIGGER_FAILED", orchestrator="rundeck", job=job_id)
    threading.Thread(target=_run, daemon=True).start()


def orchestrate(action, incident=None, result=None):
    conf = {"action": action, "result": result, "incident_id": (incident or {}).get("id"),
            "rca": (incident or {}).get("rca"), "priority": (incident or {}).get("priority")}
    trigger_airflow("aiops_remediation", conf)
    trigger_rundeck("aiops-remediation", {"action": action})
    return "ORCHESTRATION_TRIGGERED"
