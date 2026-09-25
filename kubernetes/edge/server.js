const express = require("express");
const app = express();

// Simulated baseline values (more realistic than pure random)
let cpu = 50;
let latency = 0.5;
let requests = 100;

// 🔹 Function to simulate gradual changes (good for AIOps demo)
function simulateMetrics() {
    cpu = Math.max(0, Math.min(100, cpu + (Math.random() * 10 - 5)));
    latency = Math.max(0.1, latency + (Math.random() * 0.2 - 0.1));
    requests = Math.max(10, requests + Math.floor(Math.random() * 20 - 10));
}

// 🔹 Metrics endpoint (Prometheus format)
app.get("/metrics", (req, res) => {
    simulateMetrics();

    // 🔥 LOGGING (for Kubernetes logs demo)
    console.log(
        `METRICS → CPU=${cpu.toFixed(2)}%, LATENCY=${latency.toFixed(2)}s, REQ=${requests}`
    );

    res.setHeader("Content-Type", "text/plain; version=0.0.4");

    res.end(`
# HELP edge_cpu_usage CPU usage percentage
# TYPE edge_cpu_usage gauge
edge_cpu_usage ${cpu.toFixed(2)}

# HELP edge_latency Request latency in seconds
# TYPE edge_latency gauge
edge_latency ${latency.toFixed(2)}

# HELP edge_request_rate Requests per second
# TYPE edge_request_rate gauge
edge_request_rate ${requests}

# HELP edge_healthy 1 when the edge app is healthy
# TYPE edge_healthy gauge
edge_healthy ${healthy ? 1 : 0}
`);
});

// 🔹 Self-healing demo: /break makes the liveness probe fail so Kubernetes
//    restarts the pod automatically (Kubernetes-native self-healing).
let healthy = true;

app.get("/health", (req, res) => {
    if (!healthy) return res.status(500).json({ status: "unhealthy" });
    res.json({ status: "healthy" });
});

app.get("/break", (req, res) => {
    healthy = false;
    console.log("💥 Edge app marked unhealthy - liveness probe will fail");
    res.json({ message: "Edge app broken - Kubernetes will restart the pod" });
});

// 🔹 Root endpoint
app.get("/", (req, res) => {
    res.json({
        status: "edge-app running",
        service: "kubernetes-edge",
    });
});

// 🔹 Optional: simulate spike (VERY IMPRESSIVE FOR DEMO)
app.get("/spike", (req, res) => {
    cpu = 95;
    latency = 2.5;
    requests = 300;

    console.log("🔥 SPIKE TRIGGERED!");

    res.json({ message: "Spike generated" });
});

// 🔹 Start server
const PORT = 5000;
app.listen(PORT, () => {
    console.log(`🚀 Edge App running on port ${PORT}`);
});