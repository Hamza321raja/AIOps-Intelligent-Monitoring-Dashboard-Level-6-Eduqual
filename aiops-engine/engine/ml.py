"""AIOps machine-learning layer.

* Isolation Forest  - unsupervised multi-metric anomaly detection
* Robust z-score    - statistical baseline (median / MAD) deviation + drift
* KMeans            - workload pattern recognition (LOW / NORMAL / HIGH load)
* LSTM (TensorFlow) - time-series forecast of the next CPU utilisation value
* MLflow            - every training run is tracked with params and metrics
"""
import logging
import time
from collections import deque

import numpy as np
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

import config
from engine.signals import FEATURE_NAMES

logger = logging.getLogger("aiops.ml")

try:
    import tensorflow as tf
except Exception as e:  # pragma: no cover
    tf = None
    logger.warning("TensorFlow unavailable, LSTM disabled: %s", e)

try:
    import mlflow
    mlflow.set_tracking_uri(config.MLFLOW_TRACKING_URI)
except Exception as e:  # pragma: no cover
    mlflow = None
    logger.warning("MLflow unavailable: %s", e)

N_FEATURES = 7
W = config.LSTM_WINDOW

history = deque(maxlen=720)          # raw 5-feature samples (1 hour at 5s)
scaler = StandardScaler()
iso_model = None
cluster_model = None
cluster_labels = {}
lstm_model = None
y_stats = (0.0, 1.0)          # mean/std of the LSTM target (CPU %)
trained = False
last_train_size = 0
train_count = 0
last_train_info = {}
_mlflow_ready = False


def build_features(X):
    """7 engineered features: 5 raw metrics + CPU and latency trends."""
    X = np.asarray(X, dtype=float)
    if len(X) < 2:
        return np.empty((0, N_FEATURES))
    trends = np.diff(X[:, [0, 1]], axis=0)
    return np.hstack([X[1:], trends])


def _build_lstm():
    model = tf.keras.Sequential([
        tf.keras.layers.Input(shape=(W, N_FEATURES)),
        tf.keras.layers.LSTM(32),
        tf.keras.layers.Dense(16, activation="relu"),
        tf.keras.layers.Dense(1),
    ])
    model.compile(optimizer="adam", loss="mse")
    return model


def _sequences(Xs, target):
    xs, ys = [], []
    for i in range(len(Xs) - W):
        xs.append(Xs[i:i + W])
        ys.append(target[i + W])
    return np.array(xs), np.array(ys)


def _mlflow_log(params, metrics):
    global _mlflow_ready
    if mlflow is None:
        return
    try:
        if not _mlflow_ready:
            mlflow.set_experiment(config.MLFLOW_EXPERIMENT)
            _mlflow_ready = True
        with mlflow.start_run(run_name="train-%d" % train_count):
            mlflow.log_params(params)
            mlflow.log_metrics(metrics)
    except Exception as e:
        logger.warning("MLflow logging failed: %s", e)


def train(force=False):
    global scaler, iso_model, cluster_model, cluster_labels, lstm_model, y_stats
    global trained, last_train_size, train_count, last_train_info

    n = len(history)
    if n < config.MIN_TRAINING_SAMPLES:
        return False
    if not force and trained and n - last_train_size < config.RETRAIN_EVERY and n < history.maxlen:
        return False
    if not force and trained and n == history.maxlen and time.time() - last_train_info.get("t", 0) < 300:
        return False

    t0 = time.time()
    X = build_features(list(history))
    sc = StandardScaler().fit(X)
    Xs = sc.transform(X)

    iso = IsolationForest(contamination=config.ANOMALY_CONTAMINATION, random_state=42).fit(Xs)
    km = KMeans(n_clusters=3, n_init=10, random_state=42).fit(Xs)
    # name clusters by their mean CPU utilisation so they are human readable
    order = np.argsort([X[km.labels_ == c, 0].mean() if np.any(km.labels_ == c) else 0 for c in range(3)])
    labels = {int(order[0]): "LOW_LOAD", int(order[1]): "NORMAL_LOAD", int(order[2]): "HIGH_LOAD"}

    lstm_loss = None
    if tf is not None and len(Xs) > W + 5:
        first = lstm_model is None
        if first:
            lstm_model = _build_lstm()
        # standardise the target so the network learns quickly on small data
        mu, sd = float(X[:, 0].mean()), float(max(X[:, 0].std(), 1.0))
        xs, ys = _sequences(Xs, (X[:, 0] - mu) / sd)
        epochs = config.LSTM_EPOCHS * (3 if first else 1)
        h = lstm_model.fit(xs, ys, epochs=epochs, batch_size=16, verbose=0)
        lstm_loss = float(h.history["loss"][-1])
        y_stats = (mu, sd)

    scaler, iso_model, cluster_model, cluster_labels = sc, iso, km, labels
    anomaly_rate = float(np.mean(iso.predict(Xs) == -1))
    trained = True
    last_train_size = n
    train_count += 1
    last_train_info = {
        "t": time.time(), "run": train_count, "samples": int(len(X)),
        "lstm_loss": lstm_loss, "anomaly_rate": round(anomaly_rate, 4),
        "duration_s": round(time.time() - t0, 2),
    }
    _mlflow_log(
        {"model": "IsolationForest+KMeans+LSTM", "contamination": config.ANOMALY_CONTAMINATION,
         "lstm_window": W, "lstm_epochs": config.LSTM_EPOCHS, "features": ",".join(FEATURE_NAMES) + ",cpu_trend,latency_trend"},
        {k: v for k, v in {"training_samples": len(X), "lstm_loss": lstm_loss, "train_anomaly_rate": anomaly_rate,
                           "mean_cpu_util": float(X[:, 0].mean()), "mean_p95_latency": float(X[:, 4].mean())}.items() if v is not None},
    )
    logger.info("Models trained: %s", last_train_info)
    return True


def baseline(x):
    """Robust statistical baseline (median/MAD) and drift of recent samples."""
    H = np.asarray(history, dtype=float)
    if len(H) < 30:
        return {"ready": False, "max_z": 0.0, "z": {}, "drift_score": 0.0, "drift_metric": None}
    base = H[:-12] if len(H) > 42 else H
    med = np.median(base, axis=0)
    mad = np.median(np.abs(base - med), axis=0)
    # floor the MAD so near-constant metrics do not explode the score
    floor = np.maximum(np.abs(med) * 0.05, np.array([2.0, 0.02, 0.5, 1.0, 0.05]))
    mad = np.maximum(mad, floor)
    z = 0.6745 * (np.asarray(x, dtype=float) - med) / mad
    recent = H[-12:].mean(axis=0)
    drift = 0.6745 * (recent - med) / mad
    i = int(np.argmax(np.abs(drift)))
    return {
        "ready": True,
        "z": {FEATURE_NAMES[k]: round(float(z[k]), 2) for k in range(len(FEATURE_NAMES))},
        "max_z": round(float(np.max(np.abs(z))), 2),
        "baseline": {FEATURE_NAMES[k]: round(float(med[k]), 4) for k in range(len(FEATURE_NAMES))},
        "drift_score": round(float(abs(drift[i])), 2),
        "drift_metric": FEATURE_NAMES[i],
    }


def observe(x):
    """Feeds one sample through every model and returns the combined result."""
    history.append([float(v) for v in x])
    train()
    out = {"trained": trained, "samples": len(history), "anomaly": False, "ml_anomaly": False,
           "score": 0.0, "pattern": "UNKNOWN", "predicted_cpu": None, "failure_risk": "UNKNOWN",
           "last_training": last_train_info}
    b = baseline(x)
    out["baseline"] = b
    if not trained:
        return out
    try:
        X = build_features(list(history)[-(W + 2):])
        xs = scaler.transform(X[-1:])
        score = float(iso_model.decision_function(xs)[0])
        ml_anom = bool(iso_model.predict(xs)[0] == -1)
        out["score"] = round(score, 4)
        out["ml_anomaly"] = ml_anom
        # Ensemble voting (noise reduction): raise an anomaly when the ML model
        # AND the statistical baseline agree. Isolation Forest cannot
        # extrapolate beyond its training range, so an extreme deviation
        # (robust z >= 2x threshold) is flagged on its own as well.
        z = b.get("max_z", 0)
        out["anomaly"] = bool((ml_anom and z >= config.ZSCORE_THRESHOLD) or z >= 2 * config.ZSCORE_THRESHOLD)
        out["detector"] = ("ensemble" if ml_anom and z >= config.ZSCORE_THRESHOLD
                           else "zscore-extreme" if z >= 2 * config.ZSCORE_THRESHOLD else None)
        out["pattern"] = cluster_labels.get(int(cluster_model.predict(xs)[0]), "UNKNOWN")

        if lstm_model is not None and len(X) >= W:
            seq = scaler.transform(X[-W:]).reshape(1, W, N_FEATURES)
            pred = float(lstm_model.predict(seq, verbose=0)[0][0]) * y_stats[1] + y_stats[0]
            pred = max(0.0, min(200.0, pred))
            out["predicted_cpu"] = round(pred, 2)
            cur = float(x[0])
            if pred >= 85 and pred >= cur - 5:
                out["failure_risk"] = "HIGH_RISK"
            elif pred >= 70:
                out["failure_risk"] = "MEDIUM_RISK"
            else:
                out["failure_risk"] = "NORMAL"
    except Exception as e:
        logger.error("ML inference error: %s", e)
    return out


# ---- backwards-compatible helpers used by older modules ----
def detect(x):
    r = observe(x)
    return r["anomaly"], r["score"]


def pattern(x):
    return "UNKNOWN"


def predict_failure(x):
    return "UNKNOWN"
