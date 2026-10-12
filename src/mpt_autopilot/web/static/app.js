// app.js — renders controls from the server's flag spec and streams a run.
//
// No framework and no build step: the spec is introspected from the real
// argparse parsers, so the page has nothing to hard-code about any stage.

const PAGE = JSON.parse(document.getElementById("page-spec").textContent);
const STAGES = PAGE.stages;
const LANGS = PAGE.langs;

const el = {
  nav: document.getElementById("stages"),
  form: document.getElementById("form"),
  help: document.getElementById("stage-help"),
  common: document.getElementById("common"),
  advanced: document.getElementById("advanced"),
  advancedBox: document.getElementById("advanced-box"),
  run: document.getElementById("run"),
  cancel: document.getElementById("cancel"),
  preview: document.getElementById("preview"),
  banner: document.getElementById("banner"),
  log: document.getElementById("log"),
  mptStatus: document.getElementById("mpt-status"),
  externalRuns: document.getElementById("external-runs"),
  runState: document.getElementById("run-state"),
  pipeline: document.getElementById("pipeline-map"),
};

let current = STAGES[0];
let stream = null;

// ── controls ────────────────────────────────────────────────────────────────

function field(flag) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  wrap.dataset.dest = flag.dest;
  wrap.dataset.kind = flag.kind;

  const label = document.createElement("label");
  label.textContent = flag.option + (flag.required ? " *" : "");
  label.htmlFor = "f-" + flag.dest;

  const hint = document.createElement("span");
  hint.className = "hint";
  hint.textContent = flag.help;
  hint.title = flag.help;

  if (flag.kind === "bool") {
    wrap.classList.add("switch");
    wrap.append(checkbox(flag), label, hint);
    return wrap;
  }

  if (flag.kind === "optional_value") {
    // nargs="?" is three states, so it needs two controls: the switch decides
    // whether the flag is passed at all, the box supplies an optional value.
    const row = document.createElement("div");
    row.className = "optional-value";
    const text = document.createElement("input");
    text.type = "text";
    text.id = "f-" + flag.dest;
    text.placeholder = "no value";
    text.addEventListener("input", () => {
      if (text.value.trim()) row.querySelector("input[type=checkbox]").checked = true;
      refreshPreview();
    });
    row.append(checkbox(flag), text);
    wrap.append(label, row, hint);
    return wrap;
  }

  wrap.append(label, control(flag), hint);
  return wrap;
}

function checkbox(flag) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.id = "f-" + flag.dest;
  box.addEventListener("change", refreshPreview);
  return box;
}

function chips(flag) {
  // A native <select multiple> was the whole bug: once a value was picked,
  // the only way back was ctrl+click, and nobody finds that. Chips toggle
  // by plain clicking — pick, unpick, no hidden modifier keys.
  const box = document.createElement("div");
  box.className = "chips";
  const options = flag.kind === "lang"
    ? LANGS.map((l) => [l.code, `${l.code} — ${l.label}`])
    : flag.choices.map((c) => [c, c]);
  for (const [value, text] of options) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = text;
    chip.dataset.value = value;
    chip.setAttribute("aria-pressed", "false");
    chip.addEventListener("click", () => {
      chip.setAttribute("aria-pressed", String(chip.getAttribute("aria-pressed") !== "true"));
      refreshPreview();
    });
    box.append(chip);
  }
  return box;
}

function control(flag) {
  if (flag.kind === "choice" || flag.kind === "lang") {
    if (flag.multi) return chips(flag);
    const select = document.createElement("select");
    select.id = "f-" + flag.dest;
    select.append(new Option("—", ""));
    const options = flag.kind === "lang"
      ? LANGS.map((l) => [l.code, `${l.code} — ${l.label}`])
      : flag.choices.map((c) => [c, c]);
    for (const [value, text] of options) select.append(new Option(text, value));
    select.addEventListener("change", refreshPreview);
    return select;
  }

  const input = document.createElement("input");
  input.type = flag.kind === "int" ? "number" : "text";
  input.id = "f-" + flag.dest;
  if (flag.default) input.placeholder = flag.default;
  if (flag.multi) input.placeholder = "comma-separated";
  input.addEventListener("input", refreshPreview);
  return input;
}

function renderStage(stage) {
  current = stage;
  el.help.textContent = stage.help;
  el.common.replaceChildren();
  el.advanced.replaceChildren();

  for (const flag of stage.flags) {
    (flag.advanced ? el.advanced : el.common).append(field(flag));
  }
  el.advancedBox.hidden = !el.advanced.childElementCount;
  el.advancedBox.open = false;

  for (const button of el.nav.children) {
    button.setAttribute("aria-current", String(button.dataset.stage === stage.name));
  }
  refreshPreview();
  renderPipelineMap();
}

// ── pipeline map ─────────────────────────────────────────────────────────────

// The one place the page states the pipeline outright: refill → batch →
// enrich → upload, the order pipeline.py enforces. Orientation, not
// configuration — every flag control still comes from the argparse spec.

const PIPELINE = ["refill", "batch", "enrich", "upload"];

function renderPipelineMap() {
  el.pipeline.replaceChildren();
  PIPELINE.forEach((stage, index) => {
    if (index > 0) {
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "→";
      el.pipeline.append(arrow);
    }
    const chip = document.createElement("button");
    chip.type = "button";
    chip.textContent = stage;
    const spec = STAGES.find((s) => s.name === stage);
    chip.title = spec ? spec.help : stage;
    chip.setAttribute("aria-current", String(stage === current.name));
    chip.addEventListener("click", () => {
      if (spec) renderStage(spec);
    });
    el.pipeline.append(chip);
  });
}

// ── form → flags ────────────────────────────────────────────────────────────

function collect() {
  const flags = {};
  for (const wrap of el.form.querySelectorAll(".field")) {
    const { dest, kind } = wrap.dataset;
    const flag = current.flags.find((f) => f.dest === dest);

    if (kind === "bool") {
      if (wrap.querySelector("input").checked) flags[dest] = true;
      continue;
    }

    if (kind === "optional_value") {
      if (!wrap.querySelector("input[type=checkbox]").checked) continue;
      const text = wrap.querySelector("input[type=text]").value.trim();
      flags[dest] = text || true;
      continue;
    }

    const chipsBox = wrap.querySelector(".chips");
    if (chipsBox) {
      const picked = [...chipsBox.querySelectorAll(".chip[aria-pressed=true]")]
        .map((chip) => chip.dataset.value);
      if (picked.length) flags[dest] = picked;
      continue;
    }

    const input = wrap.querySelector("input, select");
    const value = input.value.trim();
    if (!value) continue;
    flags[dest] = flag.multi ? value.split(",").map((v) => v.trim()).filter(Boolean) : value;
  }
  return flags;
}

function refreshPreview() {
  const parts = [];
  for (const [dest, value] of Object.entries(collect())) {
    const flag = current.flags.find((f) => f.dest === dest);
    if (value === true) parts.push(flag.option);
    else for (const item of [].concat(value)) parts.push(flag.option, quote(item));
  }
  el.preview.textContent = `mpt ${current.name} ${parts.join(" ")}`.trimEnd();
}

function quote(value) {
  return /[\s"']/.test(value) ? JSON.stringify(value) : value;
}

// ── run state ───────────────────────────────────────────────────────────────

function setBusy(busy) {
  el.run.disabled = busy;
  el.cancel.hidden = !busy;
  el.cancel.disabled = false;
  for (const button of el.nav.children) button.disabled = busy;
}

// ── run-state strip: what the header dot is telling you ──────────────────────

let runStage = null;
let runStartedAt = null;
let runClock = null;

function setRunState(state, text) {
  el.runState.dataset.state = state;
  el.runState.textContent = text;
}

function formatElapsed(seconds) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes ? `${minutes}m ${rest}s` : `${rest}s`;
}

function startRunClock(stage, startedAt) {
  stopRunClock();
  runStage = stage;
  runStartedAt = startedAt;
  tickRunClock();
  runClock = setInterval(tickRunClock, 1000);
}

function stopRunClock() {
  if (runClock) clearInterval(runClock);
  runClock = null;
  runStartedAt = null;
}

function tickRunClock() {
  if (runStartedAt == null) return;
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - runStartedAt));
  setRunState("running", `running: ${runStage} — ${formatElapsed(seconds)}`);
}

function banner(text, tone) {
  el.banner.textContent = text;
  el.banner.className = tone || "";
  el.banner.hidden = false;
}

function append(line) {
  const stuck = el.log.scrollTop + el.log.clientHeight >= el.log.scrollHeight - 30;
  el.log.append(line + "\n");
  if (stuck) el.log.scrollTop = el.log.scrollHeight;
}

function endTone(state) {
  if (state.cancelled) return ["cancelled", ""];
  if (state.exit_code === 0) return [`${state.stage}: done (exit code 0)`, "ok"];
  if (state.exit_code === 2) return [`${state.stage}: quota stop (exit code 2)`, "warn"];
  return [`${state.stage}: failed (exit code ${state.exit_code})`, "bad"];
}

function watch(runId) {
  if (stream) stream.close();
  stream = new EventSource(`/api/stream/${runId}`);
  setBusy(true);

  // The browser resends Last-Event-ID on its own, so a dropped connection
  // resumes where it left off rather than replaying or losing the run.
  stream.addEventListener("line", (event) => append(JSON.parse(event.data)));

  stream.addEventListener("end", (event) => {
    const state = JSON.parse(event.data);
    stream.close();
    stream = null;
    setBusy(false);
    const seconds = runStartedAt != null ? Math.round(Date.now() / 1000 - runStartedAt) : null;
    stopRunClock();
    const [text, tone] = endTone(state);
    setRunState(tone || "idle", text);
    banner(seconds != null ? `${text} in ${formatElapsed(seconds)}` : text, tone);
  });

  stream.onerror = () => {
    if (stream && stream.readyState === EventSource.CLOSED) {
      stream = null;
      setBusy(false);
      stopRunClock();
      setRunState("idle", "run state unknown");
      banner("lost connection to the server", "bad");
    }
  };
}

// ── actions ─────────────────────────────────────────────────────────────────

el.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.banner.hidden = true;
  el.log.textContent = "";
  el.run.disabled = true;

  // W-6: a rejected fetch used to escape the handler right after disabling
  // Run, leaving the page stuck until a reload.
  let response;
  try {
    response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: current.name, flags: collect() }),
    });
  } catch {
    el.run.disabled = false;
    banner("could not reach the server — is mpt web still running?", "bad");
    return;
  }
  const body = await response.json().catch(() => ({}));

  if (!response.ok) {
    el.run.disabled = false;
    banner(body.error || `could not start (HTTP ${response.status})`, "bad");
    return;
  }
  append(`$ mpt ${body.command.join(" ")}`);
  startRunClock(current.name, Date.now() / 1000);
  watch(body.run_id);
});

el.cancel.addEventListener("click", async () => {
  el.cancel.disabled = true;
  // Same W-6 shape: whatever the network does, the button comes back unless
  // the cancel itself was accepted (the end event re-enables it anyway).
  try {
    const response = await fetch("/api/cancel", { method: "POST" });
    if (!response.ok) el.cancel.disabled = false;
  } catch {
    el.cancel.disabled = false;
  }
});

// ── MPT status dot ──────────────────────────────────────────────────────────

const MPT_POLL_SECONDS = 15;

async function refreshMptStatus() {
  let text = "MPT API: unknown";
  let state = "unknown";
  try {
    const body = await (await fetch("/api/mpt-status")).json();
    if (!body.configured) {
      text = "MPT API: not configured";
    } else if (body.up) {
      // 200 = /ping answered "pong"; anything else still proves the server
      // exists (e.g. a fork that guards /ping behind its API key).
      state = "up";
      text = body.status_code === 200
        ? `MPT API: up (${body.host ?? "?"}${body.latency_ms != null ? `, ${body.latency_ms} ms` : ""})`
        : `MPT API: reachable, HTTP ${body.status_code} (${body.host ?? "?"})`;
    } else {
      state = "down";
      text = `MPT API: down (${body.host ?? "?"})`;
    }
  } catch {
    // The mpt web server itself stopped answering; keep the grey dot.
  }
  el.mptStatus.dataset.state = state;
  el.mptStatus.textContent = text;
}

// ── external runs (started in a terminal, not here) ──────────────────────────

const EXTERNAL_POLL_SECONDS = 10;

async function refreshExternalRuns() {
  let runs = [];
  try {
    const body = await (await fetch("/api/external")).json();
    runs = body.runs || [];
  } catch {
    return; // the web server itself stopped answering; keep what is shown
  }
  if (!runs.length) {
    el.externalRuns.hidden = true;
    el.externalRuns.replaceChildren();
    return;
  }
  el.externalRuns.hidden = false;
  el.externalRuns.replaceChildren(...runs.map(renderExternalRun));
}

function renderExternalRun(run) {
  // Read-only by design: the run belongs to the terminal that started it, so
  // this shows what it is doing instead of pretending it can be controlled.
  const box = document.createElement("details");
  box.className = "external-run";
  const summary = document.createElement("summary");
  const since = run.started
    ? `, started ${new Date(run.started * 1000).toLocaleTimeString()}`
    : "";
  summary.textContent = `mpt ${run.command || run.stage} — PID ${run.pid}${since}`;
  box.append(summary);
  if (run.logs.length) {
    for (const log of run.logs) {
      const pre = document.createElement("pre");
      pre.className = "external-tail";
      pre.textContent = `── ${log.name} log ──\n${log.tail}`;
      box.append(pre);
    }
  } else {
    const note = document.createElement("p");
    note.className = "help";
    note.textContent =
      "No live log: this stage keeps no file log, or it uses a different config.";
    box.append(note);
  }
  return box;
}

// ── boot ────────────────────────────────────────────────────────────────────

for (const stage of STAGES) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = stage.name;
  button.dataset.stage = stage.name;
  button.addEventListener("click", () => renderStage(stage));
  el.nav.append(button);
}

renderStage(STAGES[0]);

refreshMptStatus();
setInterval(refreshMptStatus, MPT_POLL_SECONDS * 1000);

refreshExternalRuns();
setInterval(refreshExternalRuns, EXTERNAL_POLL_SECONDS * 1000);

// A reload mid-run reattaches instead of showing an idle page.
if (PAGE.state.busy) {
  const running = STAGES.find((s) => s.name === PAGE.state.stage);
  if (running) renderStage(running);
  banner(`${PAGE.state.stage} is already running — attaching to its log`);
  startRunClock(PAGE.state.stage, PAGE.state.started_at);
  watch(PAGE.state.run_id);
}
