const API_BASE = "/api";        // app, proxied by nginx
const ENGINE = "/engine";       // AIOps engine, proxied by nginx

let totalRequests = 0;
let workers = [];

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 1, suffix = "") => (v === null || v === undefined || Number.isNaN(v) ? "-" : Number(v).toFixed(d) + suffix);

// ---------- links to the observability tools (same host, different ports) ----------
(function links() {
  const h = window.location.hostname;
  const tools = [["Grafana", 3001], ["Prometheus", 9090], ["Alertmanager", 9093], ["Jaeger", 16686], ["MLflow", 5000], ["Airflow", 8085], ["Rundeck", 4440]];
  $("links").innerHTML = tools.map(([n, p]) => `<a href="http://${h}:${p}" target="_blank" rel="noopener">${n}</a>`).join("");
})();

// ---------- load generator ----------
function updateUI() {
  $("counter").innerText = totalRequests;
  $("workers").innerText = workers.length;
}

async function checkBackend() {
  try {
    const res = await fetch(`${API_BASE}/health`);
    $("status").innerText = res.ok ? "Healthy" : `Unhealthy (${res.status})`;
    $("status").className = res.ok ? "ok" : "bad";
  } catch (e) {
    $("status").innerText = "Down";
    $("status").className = "bad";
  }
}

function createWorker(rps) {
  const worker = setInterval(() => {
    for (let i = 0; i < rps; i++) {
      fetch(`${API_BASE}/load`).catch(() => {});
      totalRequests++;
    }
    updateUI();
  }, 1000);
  workers.push(worker);
}

function startLoad(rps) { createWorker(rps); updateUI(); }
function stopLoad() { workers.forEach((w) => clearInterval(w)); workers = []; updateUI(); }

// ---------- fault injection ----------
async function chaos(path) {
  try {
    const r = await fetch(`${API_BASE}/chaos/${path}`);
    $("chaos-state").innerText = "Injected: " + JSON.stringify(await r.json());
  } catch (e) {
    $("chaos-state").innerText = "Fault injection failed: " + e;
  }
}

async function chaosStatus() {
  try {
    const s = await (await fetch(`${API_BASE}/chaos/status`)).json();
    const active = [];
    if (s.broken) active.push(s.persistent ? "persistent failure" : "app failure");
    if (s.cpuIntensity > 0) active.push(`CPU ${Math.round(s.cpuIntensity * 100)}% (${s.cpuSecondsLeft}s left)`);
    if (s.errorRate > 0.01) active.push(`errors ${Math.round(s.errorRate * 100)}%`);
    if (s.extraLatencyMs > 0) active.push(`latency +${s.extraLatencyMs}ms`);
    if (s.memoryLeakMb > 0) active.push(`memory leak ${s.memoryLeakMb} MB`);
    $("chaos-state").innerText = active.length ? "Active: " + active.join(", ") : "No faults active (RSS " + s.rssMb + " MB)";
  } catch (e) { /* app may be restarting */ }
}

// ---------- AIOps engine status ----------
function kv(el, rows) {
  $(el).innerHTML = rows.map(([k, v, cls]) => `<tr><td>${esc(k)}</td><td class="${cls || ""}">${esc(v)}</td></tr>`).join("");
}

async function engineStatus() {
  let st;
  try {
    st = await (await fetch(`${ENGINE}/status`)).json();
  } catch (e) {
    $("k-health").innerText = "engine offline";
    return;
  }
  const s = st.signals || {}, ml = st.ml || {}, sla = st.sla || {}, stats = st.stats || {}, safety = st.safety || {};
  $("k-health").innerText = num(st.health_score, 0);
  $("k-sla").innerText = num(sla.availability_pct, 2, "%");
  $("k-sla").className = sla.compliant === false ? "bad" : "ok";
  $("k-budget").innerText = num(sla.error_budget_remaining_pct, 0, "%");
  $("k-open").innerText = st.open_incidents ?? "-";
  $("k-mttr").innerText = num(stats.mttr_s, 0, "s");
  $("k-noise").innerText = num((stats.noise_reduction_ratio || 0) * 100, 0, "%");
  $("k-comp").innerText = num((st.compliance || {}).score, 0, "%");

  const b = ml.baseline || {};
  kv("ml", [
    ["Models trained", ml.trained ? `yes (${(ml.last_training || {}).run || 0} runs, ${ml.samples} samples)` : `warming up (${ml.samples || 0}/60 samples)`],
    ["Anomaly (IsolationForest + z-score)", ml.confirmed_anomaly ? "ANOMALY" : "normal", ml.confirmed_anomaly ? "bad" : "ok"],
    ["Anomaly score / max z", `${num(ml.score, 3)} / ${num(b.max_z, 1)}`],
    ["Workload pattern (KMeans)", ml.pattern || "-"],
    ["CPU now -> LSTM forecast", `${num(s.cpu_util, 0, "%")} -> ${num(ml.predicted_cpu, 0, "%")}`],
    ["Failure risk", ml.failure_risk || "-", ml.failure_risk === "HIGH_RISK" ? "bad" : ""],
    ["Capacity forecast", `${(st.capacity || {}).status || "-"} (${num((st.capacity || {}).forecast_cpu_util, 0, "%")})`],
    ["p95 latency / errors", `${num(s.p95_latency, 2, "s")} / ${num(s.error_pct, 1, "%")}`],
    ["Drift vs baseline", `${num(b.drift_score, 1)} MAD (${b.drift_metric || "-"})`],
  ]);

  const d = st.decision || {};
  kv("decision", [
    ["Last decision", d.action ? `${d.action} (${d.mode})` : "none"],
    ["Incident / root cause", d.incident ? `${d.incident} ${d.rca}` : "-"],
    ["Confidence", num(d.confidence, 2)],
    ["Reason", d.reason || "-"],
    ["Recommendation", d.recommendation || "-"],
    ["Auto-remediation threshold", num(safety.min_confidence, 2)],
    ["Circuit breaker", safety.circuit_breaker_open ? "OPEN (automation paused)" : "closed", safety.circuit_breaker_open ? "bad" : "ok"],
    ["Remediation running", safety.remediation_in_progress ? "yes" : "no"],
    ["App scale level", safety.scale_level],
    ["Firing Prometheus alerts", (st.active_alerts || []).map((a) => `${a.priority || ""} ${a.alert}`).join(", ") || "none"],
  ]);

  const incs = st.incidents || [];
  $("incidents").innerHTML = "<tr><th>ID</th><th>Priority</th><th>Root cause</th><th>Conf.</th><th>Status</th><th>Evidence</th><th>Blast radius</th><th>Actions</th><th></th></tr>" +
    (incs.length ? incs.map((i) => {
      const acts = (i.actions || []).map((a) => `${a.action}:${a.result}${a.rollback ? " (rolled back)" : ""}`).join(", ");
      const canApprove = i.status === "ESCALATED" && i.decision && ["RESTART", "PREEMPTIVE_RESTART", "SCALE_UP", "SCALE_DOWN", "REVERT_CONFIG"].includes(i.decision.action);
      return `<tr class="p-${esc(i.priority)}"><td>${esc(i.id)}</td><td>${esc(i.priority)}</td><td>${esc(i.rca)}</td><td>${num(i.confidence, 2)}</td>
        <td class="st-${esc(i.status)}">${esc(i.status)}${i.ttr_s ? " (" + esc(i.ttr_s) + "s)" : ""}</td>
        <td>${esc((i.evidence || []).join("; "))}</td><td>${esc((i.blast_radius || []).join(", "))}</td>
        <td>${esc(acts || (i.decision ? i.decision.mode : ""))}</td>
        <td>${canApprove ? `<button onclick="approve('${esc(i.id)}')">Approve ${esc(i.decision.action)}</button>` : ""}</td></tr>`;
    }).join("") : "<tr><td colspan=9 class='muted'>No incidents yet</td></tr>");

  const m = st.maintenance || [];
  $("maintenance").innerHTML = m.length ? m.map((x) =>
    `<div class="maint"><b class="${x.risk === "HIGH" ? "bad" : ""}">${esc(x.risk)}</b> ${esc(x.component)}: ${esc(x.evidence)}<br>
     action <b>${esc(x.action)}</b>, window <b>${esc(x.window)}</b>${x.predicted_failure_in_min != null ? ", failure in ~" + esc(x.predicted_failure_in_min) + " min" : ""}</div>`).join("")
    : "No risks predicted";

  $("sla-line").innerText = `SLO: availability >= ${sla.slo_availability ?? "-"}%, p95 <= ${sla.slo_p95_s ?? "-"}s over ${sla.window || "1h"} - currently ${sla.compliant === false ? "BREACHED" : "compliant"}`;

  $("audit").innerHTML = "<tr><th>Time</th><th>Actor</th><th>Event</th><th>Details</th></tr>" +
    (st.audit || []).map((a) => {
      const { ts, actor, event, type, ...rest } = a;
      delete rest.steps;
      return `<tr><td>${esc(ts)}</td><td>${esc(actor)}</td><td>${esc(event)}</td><td>${esc(JSON.stringify(rest)).slice(0, 220)}</td></tr>`;
    }).join("");
}

async function approve(id) {
  const r = await fetch(`${ENGINE}/approve?incident=${encodeURIComponent(id)}`, { method: "POST" });
  alert(`Approval sent: ${JSON.stringify(await r.json())}`);
}

async function loadReport() {
  $("report").innerText = "Generating...";
  try {
    $("report").innerText = await (await fetch(`${ENGINE}/report.md?actor=operator`)).text();
  } catch (e) {
    $("report").innerText = "Report failed: " + e;
  }
}

setInterval(checkBackend, 3000);
setInterval(chaosStatus, 3000);
setInterval(engineStatus, 3000);
checkBackend();
chaosStatus();
engineStatus();
