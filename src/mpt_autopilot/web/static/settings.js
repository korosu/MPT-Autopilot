// settings.js — the Settings page: MPT facts, restart, live logs.
//
// Mirrors app.js's shape (dot wording, SSE handling, stick-to-bottom log)
// but is standalone on purpose: app.js builds the stage form from a page
// spec that this page has no use for.

const el = (id) => document.getElementById(id);

const MPT_POLL_SECONDS = 15;
let detailsTimer = null;
let logStream = null; // the EventSource while attached

// ── the status card ─────────────────────────────────────────────────────────

function setDot(state, text) {
  const dot = el("mpt-status");
  dot.dataset.state = state;
  dot.textContent = text;
}

// Same wording as the main page's dot, so the two never disagree.
function dotText(body) {
  if (!body.configured) return ["unknown", "MPT API: not configured"];
  if (body.up) {
    const via = body.via === "tasks" ? "/api/v1/tasks" : body.via === "ping" ? "/ping" : "";
    return [
      "up",
      `MPT API: up (${body.host ?? "?"}${via ? `, ${via}` : ""}` +
        `${body.latency_ms != null ? `, ${body.latency_ms} ms` : ""})`,
    ];
  }
  if (body.reachable) {
    const why = body.status_code === 401 || body.status_code === 403 ? "needs x-api-key" : "not MoneyPrinterTurbo";
    return ["warn", `MPT API: ${why} — HTTP ${body.status_code ?? "?"} (${body.host ?? "?"})`];
  }
  return ["down", `MPT API: down (${body.host ?? "?"})`];
}

function fact(term, value) {
  const dt = document.createElement("dt");
  dt.textContent = term;
  const dd = document.createElement("dd");
  dd.textContent = value;
  el("mpt-facts").append(dt, dd);
}

function renderDetails(body) {
  const [state, text] = dotText(body);
  setDot(state, text);

  el("mpt-facts").replaceChildren();
  fact("State", text.slice("MPT API: ".length));
  if (body.version) fact("Version", `${body.version} — ${body.title}`);
  fact("Deployment", DEPLOYMENTS[body.deployment] || body.deployment);
  if (body.api_url) fact("API URL", body.api_url);
  if (body.status_code != null) fact("HTTP", String(body.status_code));
  if (body.latency_ms != null) fact("Latency", `${body.latency_ms} ms`);
}

const DEPLOYMENTS = {
  unconfigured: "no batch.api_url in config",
  "remote-host": "another machine (not localhost)",
  "port-free": "nothing listens on that port",
  local: "a local process on this machine",
  unknown: "could not tell (psutil missing or denied)",
};

async function refreshDetails() {
  try {
    const body = await (await fetch("/api/mpt/details")).json();
    renderDetails(body);
    // A reload mid-stream must re-attach, not start a second command.
    if (body.logs_live && !logStream) attachLogs();
  } catch {
    // the web server itself stopped answering; keep what is shown
  }
}

el("recheck").addEventListener("click", () => {
  el("recheck").disabled = true;
  refreshDetails().finally(() => {
    el("recheck").disabled = false;
  });
});

// ── restart ─────────────────────────────────────────────────────────────────

const restartButton = el("restart-mpt");
if (restartButton) {
  const output = el("restart-output");
  restartButton.addEventListener("click", async () => {
    const command = restartButton.closest(".settings-card").querySelector("code.command").textContent;
    if (!confirm(`Restart MoneyPrinterTurbo?\n\n${command}`)) return;
    restartButton.disabled = true;
    output.hidden = false;
    output.textContent = "running…";
    output.className = "";
    let body = null;
    try {
      const response = await fetch("/api/mpt/restart", { method: "POST" });
      body = await response.json().catch(() => null);
      if (!response.ok) {
        output.textContent = body?.error || `restart failed (HTTP ${response.status})`;
        output.className = "bad";
      } else {
        output.textContent = body.output.length ? body.output.join("\n") : "(no output)";
        output.className = body.ok ? "ok" : "bad";
      }
    } catch {
      output.textContent = "could not reach the server — is mpt web still running?";
      output.className = "bad";
    }
    restartButton.disabled = false;
    refreshDetails(); // a restart changes what the card should say
  });
}

// ── live logs ───────────────────────────────────────────────────────────────

function appendLog(line) {
  const pane = el("mpt-log");
  const stuck = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 30;
  pane.append(line + "\n");
  if (stuck) pane.scrollTop = pane.scrollHeight;
}

function attachLogs() {
  if (logStream) logStream.close();
  logStream = new EventSource("/api/mpt/logs/stream");
  el("logs-start").hidden = true;
  el("logs-stop").hidden = false;
  logStream.addEventListener("line", (event) => appendLog(JSON.parse(event.data)));
  logStream.addEventListener("end", () => {
    logStream.close();
    logStream = null;
    el("logs-start").hidden = false;
    el("logs-stop").hidden = true;
    appendLog("[stream ended]");
  });
  logStream.onerror = () => {
    if (logStream && logStream.readyState === EventSource.CLOSED) {
      logStream = null;
      el("logs-start").hidden = false;
      el("logs-stop").hidden = true;
      appendLog("[lost connection to the server]");
    }
  };
}

el("logs-start")?.addEventListener("click", async () => {
  const start = el("logs-start");
  start.disabled = true;
  let response;
  try {
    response = await fetch("/api/mpt/logs/start", { method: "POST" });
  } catch {
    start.disabled = false;
    appendLog("[could not reach the server — is mpt web still running?]");
    return;
  }
  start.disabled = false;
  if (response.status === 400) {
    const body = await response.json().catch(() => ({}));
    appendLog(`[ERROR] ${body.error || "not configured"}`);
    return;
  }
  // 202 = just started; 409 = already running (a reload, a second tab) —
  // either way there is a stream to attach to.
  attachLogs();
});

el("logs-stop")?.addEventListener("click", async () => {
  const stop = el("logs-stop");
  stop.disabled = true;
  try {
    const response = await fetch("/api/mpt/logs/stop", { method: "POST" });
    if (!response.ok) stop.disabled = false;
  } catch {
    stop.disabled = false; // the end event re-enables it anyway
  }
});

// ── the setup wizard ─────────────────────────────────────────────────────────

const setupForm = el("mpt-setup");
if (setupForm) {
  const type = el("setup-type");
  const name = el("setup-name");
  const startRow = el("setup-start-row");
  const start = el("setup-start");
  const error = el("setup-error");

  // Each type asks only for the fields it derives commands from; the rest
  // stay out of the way — and out of the POST.
  function syncRows() {
    startRow.hidden = type.value !== "tmux";
  }
  type.addEventListener("change", syncRows);
  syncRows(); // the server pre-renders the right rows; this covers a reset

  setupForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const payload = { type: type.value, name: name.value.trim() };
    if (type.value === "tmux") payload.start_command = start.value.trim();
    const save = el("setup-save");
    save.disabled = true;
    let response;
    try {
      response = await fetch("/api/mpt/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      save.disabled = false;
      error.hidden = false;
      error.textContent = "could not reach the server — is mpt web still running?";
      return;
    }
    save.disabled = false;
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      error.hidden = false;
      error.textContent = body.error || `save failed (HTTP ${response.status})`;
      return;
    }
    // The server re-renders the whole page from the saved choice: the
    // command cards light up with the derived lines.
    location.reload();
  });
}

// ── boot ────────────────────────────────────────────────────────────────────

refreshDetails();
detailsTimer = setInterval(refreshDetails, MPT_POLL_SECONDS * 1000);
