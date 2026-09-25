// ===============================
// 🔥 1. OPENTELEMETRY (FIXED)
// ===============================
const { NodeSDK } = require("@opentelemetry/sdk-node");
const { OTLPTraceExporter } = require("@opentelemetry/exporter-trace-otlp-http");
const { getNodeAutoInstrumentations } = require("@opentelemetry/auto-instrumentations-node");
const { resourceFromAttributes } = require("@opentelemetry/resources");
const { SemanticResourceAttributes } = require("@opentelemetry/semantic-conventions");

// 🔥 force proper instrumentation
const instrumentations = getNodeAutoInstrumentations({
  "@opentelemetry/instrumentation-http": { enabled: true },
  "@opentelemetry/instrumentation-express": { enabled: true },
});

const traceExporter = new OTLPTraceExporter({
  url: "http://otel-collector:4318/v1/traces",
});

const sdk = new NodeSDK({
  traceExporter,
  instrumentations,
  resource: resourceFromAttributes({
    [SemanticResourceAttributes.SERVICE_NAME]: "aiops-app",
  }),
});

// 🔥 IMPORTANT: start synchronously (NO async)
sdk.start();
console.log("✅ OpenTelemetry started");


// ===============================
// 🔥 2. IMPORTS
// ===============================
const express = require("express");
const client = require("prom-client");
const winston = require("winston");
const fs = require("fs");
const { trace, context } = require("@opentelemetry/api");


// ===============================
// 🔥 3. LOGGING SETUP (LOKI)
// ===============================
const logDir = "/app/logs";

if (!fs.existsSync(logDir)) {
  fs.mkdirSync(logDir, { recursive: true });
}

const logger = winston.createLogger({
  level: "info",
  format: winston.format.json(),
  transports: [
    new winston.transports.File({
      filename: `${logDir}/app.log`,
    }),
    new winston.transports.Console(),
  ],
});


// ===============================
// 🔥 4. APP SETUP
// ===============================
const app = express();
app.use(express.json());


// ===============================
// 🔥 5. PROMETHEUS METRICS
// ===============================
client.collectDefaultMetrics();

const httpRequestCounter = new client.Counter({
  name: "http_requests_total",
  help: "Total HTTP requests",
  labelNames: ["method", "route", "status"],
});

const httpRequestDuration = new client.Histogram({
  name: "http_request_duration_seconds",
  help: "Request duration",
  labelNames: ["method", "route", "status"],
  buckets: [0.05, 0.1, 0.25, 0.5, 1, 1.5, 2, 3, 5],
});

const chaosGauge = new client.Gauge({
  name: "app_chaos_active",
  help: "1 when a fault-injection experiment is active",
  labelNames: ["type"],
});


// ===============================
// 🔥 5b. FAULT INJECTION (CHAOS) STATE
// Used in the demo to create real failures that the AIOps engine must
// detect and remediate. All state is in memory, so a restart clears it
// (except 'persist', which simulates a failure a restart cannot fix).
// ===============================
const PERSIST_FILE = `${logDir}/.chaos_persist`;
const chaos = {
  broken: fs.existsSync(PERSIST_FILE),
  errorRate: 0.005,        // baseline 0.5% simulated errors
  extraLatencyMs: 0,
  cpuIntensity: 0,
  cpuUntil: 0,
  leak: [],
  leakTimer: null,
};

function updateChaosGauges() {
  chaosGauge.set({ type: "broken" }, chaos.broken ? 1 : 0);
  chaosGauge.set({ type: "errors" }, chaos.errorRate > 0.01 ? 1 : 0);
  chaosGauge.set({ type: "latency" }, chaos.extraLatencyMs > 0 ? 1 : 0);
  chaosGauge.set({ type: "cpu" }, Date.now() < chaos.cpuUntil ? 1 : 0);
  chaosGauge.set({ type: "memory_leak" }, chaos.leakTimer ? 1 : 0);
}
updateChaosGauges();

// CPU burn: busy-loop `intensity` of every 100ms slice (0.8 = ~80% of one core)
setInterval(() => {
  if (Date.now() < chaos.cpuUntil && chaos.cpuIntensity > 0) {
    const end = Date.now() + 100 * chaos.cpuIntensity;
    while (Date.now() < end) { Math.sqrt(Math.random()); }
  }
}, 100);


// ===============================
// 🔥 6. REQUEST LOGGING + TRACE
// ===============================
const UNTRACKED = new Set(["/metrics", "/favicon.ico"]);

app.use((req, res, next) => {
  // keep the SLI clean: scrapes and demo-control calls are not user traffic
  if (UNTRACKED.has(req.path) || req.path.startsWith("/chaos")) return next();

  const start = Date.now();
  const end = httpRequestDuration.startTimer();
  const tracer = trace.getTracer("http-middleware");
  const span = tracer.startSpan(`${req.method} ${req.path}`);

  res.on("finish", () => {
    const duration = Date.now() - start;
    const labels = {
      method: req.method,
      route: req.route?.path || req.path,
      status: res.statusCode,
    };

    httpRequestCounter.inc(labels);
    end(labels);

    span.setAttribute("http.method", req.method);
    span.setAttribute("http.route", req.path);
    span.setAttribute("http.status_code", res.statusCode);
    span.end();

    logger.info({
      type: "http_request",
      method: req.method,
      route: req.path,
      status: res.statusCode,
      duration_ms: duration,
    });
  });

  next();
});

// Chaos control endpoints bypass the "broken" state so the demo can reset it
function isChaosRoute(p) { return p.startsWith("/chaos"); }

app.use((req, res, next) => {
  if (chaos.broken && !isChaosRoute(req.path) && !UNTRACKED.has(req.path)) {
    logger.error({ type: "application_error", message: "Service in failed state (injected)", route: req.path });
    return res.status(503).json({ error: "Service unavailable (injected failure)" });
  }
  next();
});


// ===============================
// 🔥 7. ROUTES (WITH SPANS)
// ===============================
app.get("/", (req, res) => {
  const span = trace.getTracer("routes").startSpan("home-endpoint");
  span.end();
  res.json({ status: "AIOps App Running", service: "aiops-app" });
});

app.get("/health", (req, res) => {
  res.json({ status: "healthy", uptime_s: Math.round(process.uptime()) });
});

app.get("/load", async (req, res) => {
  const span = trace.getTracer("routes").startSpan("load-endpoint");

  // simulated downstream call (e.g. database) - child span shows in Jaeger
  const db = trace.getTracer("routes").startSpan("db-query", undefined,
    trace.setSpan(context.active(), span));
  const delay = 50 + Math.random() * 300 + chaos.extraLatencyMs;
  await new Promise((r) => setTimeout(r, delay));
  db.setAttribute("db.system", "simulated");
  db.end();

  if (Math.random() < chaos.errorRate) {
    logger.error({ type: "application_error", message: "Simulated failure", delay });
    span.recordException(new Error("Simulated failure"));
    span.end();
    return res.status(500).json({ error: "Simulated failure", delay });
  }

  span.end();
  res.json({ message: "OK", delay });
});

app.get("/trace-test", (req, res) => {
  const span = trace.getTracer("routes").startSpan("trace-test-endpoint");
  logger.info({ type: "trace_test", message: "Trace endpoint hit" });
  span.end();
  res.json({ traced: true });
});


// ===============================
// 🔥 7b. CHAOS ENDPOINTS (demo fault injection)
// ===============================
app.get("/chaos/status", (req, res) => {
  res.json({
    broken: chaos.broken,
    persistent: fs.existsSync(PERSIST_FILE),
    errorRate: chaos.errorRate,
    extraLatencyMs: chaos.extraLatencyMs,
    cpuIntensity: Date.now() < chaos.cpuUntil ? chaos.cpuIntensity : 0,
    cpuSecondsLeft: Math.max(0, Math.round((chaos.cpuUntil - Date.now()) / 1000)),
    memoryLeakMb: chaos.leak.length,
    rssMb: Math.round(process.memoryUsage().rss / 1048576),
  });
});

// Scenario 1: service failure a restart can fix (?persist=1 -> restart cannot fix it)
app.get("/chaos/break", (req, res) => {
  chaos.broken = true;
  if (req.query.persist === "1") fs.writeFileSync(PERSIST_FILE, "1");
  logger.warn({ type: "chaos", action: "break", persist: req.query.persist === "1" });
  updateChaosGauges();
  res.json({ broken: true, persistent: req.query.persist === "1" });
});

app.get("/chaos/errors", (req, res) => {
  chaos.errorRate = Math.min(1, Math.max(0, parseFloat(req.query.rate || "0.3")));
  logger.warn({ type: "chaos", action: "errors", rate: chaos.errorRate });
  updateChaosGauges();
  res.json({ errorRate: chaos.errorRate });
});

app.get("/chaos/latency", (req, res) => {
  chaos.extraLatencyMs = Math.max(0, parseInt(req.query.ms || "1500", 10));
  logger.warn({ type: "chaos", action: "latency", ms: chaos.extraLatencyMs });
  updateChaosGauges();
  res.json({ extraLatencyMs: chaos.extraLatencyMs });
});

// Scenario: CPU saturation (intensity 0.8 is fixed by scale-up, 0.98 is not)
app.get("/chaos/cpu", (req, res) => {
  chaos.cpuIntensity = Math.min(0.99, Math.max(0.05, parseFloat(req.query.intensity || "0.8")));
  const seconds = Math.max(10, parseInt(req.query.seconds || "180", 10));
  chaos.cpuUntil = Date.now() + seconds * 1000;
  logger.warn({ type: "chaos", action: "cpu", intensity: chaos.cpuIntensity, seconds });
  updateChaosGauges();
  res.json({ cpuIntensity: chaos.cpuIntensity, seconds });
});

// Scenario: memory leak (~1 MB every 2s) for predictive maintenance
app.get("/chaos/leak", (req, res) => {
  if (!chaos.leakTimer) {
    chaos.leakTimer = setInterval(() => {
      chaos.leak.push(Buffer.alloc(1048576, 1));
    }, 2000);
  }
  logger.warn({ type: "chaos", action: "memory_leak" });
  updateChaosGauges();
  res.json({ memoryLeak: true });
});

app.get("/chaos/reset", (req, res) => {
  chaos.broken = false;
  chaos.errorRate = 0.005;
  chaos.extraLatencyMs = 0;
  chaos.cpuUntil = 0;
  if (chaos.leakTimer) clearInterval(chaos.leakTimer);
  chaos.leakTimer = null;
  chaos.leak = [];
  try { fs.unlinkSync(PERSIST_FILE); } catch (e) { /* not present */ }
  logger.info({ type: "chaos", action: "reset" });
  updateChaosGauges();
  res.json({ reset: true });
});


// ===============================
// 🔥 8. METRICS ENDPOINT
// ===============================
app.get("/metrics", async (req, res) => {
  res.set("Content-Type", client.register.contentType);
  res.end(await client.register.metrics());
});


// ===============================
// 🔥 9. START SERVER
// ===============================
app.listen(3000, "0.0.0.0", () => {
  logger.info({ type: "startup", message: "🚀 App running on port 3000", broken_on_start: chaos.broken });
  console.log("🚀 App running on port 3000");
});
