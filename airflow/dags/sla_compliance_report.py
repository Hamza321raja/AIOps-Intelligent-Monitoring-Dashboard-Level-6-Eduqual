"""Automated SLA compliance reporting: every 15 minutes Airflow asks the AIOps
engine to generate the SLA/compliance report (JSON + Markdown files are
written to logs/reports/ and the event is recorded in the audit trail)."""
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator

ENGINE = "http://aiops-engine:8000"


def generate_report(window, **ctx):
    rep = requests.get(ENGINE + "/report", params={"window": window, "actor": "airflow"}, timeout=30).json()
    sli = rep.get("sli", {})
    print("SLA %s: compliant=%s availability=%s p95=%s error_budget=%s" % (
        window, rep.get("compliant"), sli.get("availability_pct"), sli.get("p95_latency_s"),
        rep.get("error_budget_remaining_pct")))
    print("files: %s" % rep.get("files"))
    return {"compliant": rep.get("compliant"), "breaches": rep.get("breaches")}


def check_breach(**ctx):
    r1 = ctx["ti"].xcom_pull(task_ids="report_1h") or {}
    if not r1.get("compliant", True):
        requests.post(ENGINE + "/audit", json={"event": "SLA_BREACH_REPORTED", "actor": "airflow",
                                               "breaches": r1.get("breaches"), "run_id": ctx["run_id"]}, timeout=5)
        print("SLA breach recorded in audit trail: %s" % r1.get("breaches"))
    else:
        print("SLA compliant")


with DAG(
    dag_id="sla_compliance_report",
    description="Scheduled SLA / compliance report generation",
    start_date=datetime(2025, 1, 1),
    schedule="*/15 * * * *",
    catchup=False,
    default_args={"retries": 1, "retry_delay": timedelta(seconds=30)},
    tags=["aiops", "sla", "compliance"],
) as dag:
    r1 = PythonOperator(task_id="report_1h", python_callable=generate_report, op_kwargs={"window": "1h"})
    r24 = PythonOperator(task_id="report_24h", python_callable=generate_report, op_kwargs={"window": "24h"})
    chk = PythonOperator(task_id="check_breach", python_callable=check_breach)
    [r1, r24] >> chk
