import numpy as np
import logging

logger = logging.getLogger("spark")


def run_spark_analysis(data):

    # ===============================
    # 🔹 SIMULATED DISTRIBUTED ANALYSIS
    # ===============================
    if not data or len(data) < 10:
        return {
            "cluster": "INSUFFICIENT_DATA",
            "insight": "Not enough data for batch analysis"
        }

    data = np.array(data)

    cpu = data[:, 0]
    latency = data[:, 1]
    requests = data[:, 2]

    # ===============================
    # 🔹 AGGREGATIONS (Spark-style)
    # ===============================
    avg_cpu = np.mean(cpu)
    max_cpu = np.max(cpu)
    avg_latency = np.mean(latency)
    total_requests = np.sum(requests)

    # ===============================
    # 🔹 PATTERN DETECTION
    # ===============================
    if avg_cpu > 70 and avg_latency > 1:
        cluster = "HIGH_LOAD_CLUSTER"
        insight = "Sustained high CPU and latency"

    elif total_requests > 1000:
        cluster = "TRAFFIC_CLUSTER"
        insight = "High incoming traffic"

    elif avg_cpu < 20:
        cluster = "IDLE_CLUSTER"
        insight = "Underutilized resources"

    else:
        cluster = "NORMAL_CLUSTER"
        insight = "System operating normally"

    # ===============================
    # 🔹 OUTPUT (ANALYTICS RESULT)
    # ===============================
    result = {
        "cluster": cluster,
        "avg_cpu": round(avg_cpu, 2),
        "max_cpu": round(max_cpu, 2),
        "avg_latency": round(avg_latency, 2),
        "total_requests": int(total_requests),
        "insight": insight
    }

    logger.info(f"[SPARK] Analysis Result: {result}")

    return result