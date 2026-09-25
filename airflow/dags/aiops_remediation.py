"""Orchestrated incident-response workflow, triggered by the AIOps engine
after every automated action (POST /api/v1/dags/aiops_remediation/dagRuns).

validate_request -> verify_service_health -> collect_evidence -> publish_audit
"""
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator

ENGINE = "http://aiops-engine:8000"
APP_HEALTH = "http://app:3000/health"


def validate_request(**ctx):
    conf = ctx["dag_run"].conf or {}
    for key in ("action", "incident_id"):
        if not conf.get(key):
            raise ValueError("missing '%s' in dag_run.conf" % key)
    print("Incident %s: action=%s result=%s rca=%s priority=%s" % (
        conf["incident_id"], conf["action"], conf.get("result"), conf.get("rca"), conf.get("priority")))
    return conf


def verify_service_health(**ctx):
    code = None
    try:
        code = requests.get(APP_HEALTH, timeout=5).status_code
    except Exception as e:
        print("health check failed: %s" % e)
    print("post-remediation health check: %s" % code)
    return {"health_code": code, "healthy": code == 200}


def collect_evidence(**ctx):
    conf = ctx["dag_run"].conf or {}
    incidents = requests.get(ENGINE + "/incidents", timeout=5).json()
    inc = next((i for i in incidents if i.get("id") == conf.get("incident_id")), {})
    evidence = {"status": inc.get("status"), "evidence": inc.get("evidence"),
                "blast_radius": inc.get("blast_radius"), "actions": len(inc.get("actions", []))}
    print("incident evidence: %s" % evidence)
    return evidence


def publish_audit(**ctx):
    ti = ctx["ti"]
    conf = ctx["dag_run"].conf or {}
    health = ti.xcom_pull(task_ids="verify_service_health") or {}
    evidence = ti.xcom_pull(task_ids="collect_evidence") or {}
    requests.post(ENGINE + "/audit", json={
        "event": "WORKFLOW_COMPLETED", "actor": "airflow", "dag": "aiops_remediation",
        "run_id": ctx["run_id"], "incident": conf.get("incident_id"), "action": conf.get("action"),
        "remediation_result": conf.get("result"), "post_check_healthy": health.get("healthy"),
        "incident_status": evidence.get("status"),
    }, timeout=5)


with DAG(
    dag_id="aiops_remediation",
    description="Orchestrated response procedure after AIOps automated remediation",
    start_date=datetime(2025, 1, 1),
    schedule=None,
    catchup=False,
    default_args={"retries": 2, "retry_delay": timedelta(seconds=10)},
    tags=["aiops", "remediation", "runbook"],
) as dag:
    t1 = PythonOperator(task_id="validate_request", python_callable=validate_request)
    t2 = PythonOperator(task_id="verify_service_health", python_callable=verify_service_health)
    t3 = PythonOperator(task_id="collect_evidence", python_callable=collect_evidence)
    t4 = PythonOperator(task_id="publish_audit", python_callable=publish_audit)
    t1 >> t2 >> t3 >> t4
