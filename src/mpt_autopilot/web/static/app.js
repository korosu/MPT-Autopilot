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
    text.placeholder = "без значения";
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

function control(flag) {
  if (flag.kind === "choice" || flag.kind === "lang") {
    const select = document.createElement("select");
    select.id = "f-" + flag.dest;
    select.multiple = flag.multi;
    if (!flag.multi) select.append(new Option("—", ""));
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
  if (flag.multi) input.placeholder = "через запятую";
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

    const input = wrap.querySelector("input, select");
    if (input.multiple) {
      const picked = [...input.selectedOptions].map((o) => o.value).filter(Boolean);
      if (picked.length) flags[dest] = picked;
      continue;
    }

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
  if (state.cancelled) return ["отменено", ""];
  if (state.exit_code === 0) return [`${state.stage}: готово (код 0)`, "ok"];
  if (state.exit_code === 2) return [`${state.stage}: остановлено по квоте (код 2)`, "warn"];
  return [`${state.stage}: ошибка (код ${state.exit_code})`, "bad"];
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
    banner(...endTone(state));
  });

  stream.onerror = () => {
    if (stream && stream.readyState === EventSource.CLOSED) {
      stream = null;
      setBusy(false);
      banner("соединение с сервером потеряно", "bad");
    }
  };
}

// ── actions ─────────────────────────────────────────────────────────────────

el.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.banner.hidden = true;
  el.log.textContent = "";
  el.run.disabled = true;

  const response = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage: current.name, flags: collect() }),
  });
  const body = await response.json().catch(() => ({}));

  if (!response.ok) {
    el.run.disabled = false;
    banner(body.error || `не удалось запустить (HTTP ${response.status})`, "bad");
    return;
  }
  append(`$ mpt ${body.command.join(" ")}`);
  watch(body.run_id);
});

el.cancel.addEventListener("click", async () => {
  el.cancel.disabled = true;
  const response = await fetch("/api/cancel", { method: "POST" });
  if (!response.ok) el.cancel.disabled = false;
});

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

// A reload mid-run reattaches instead of showing an idle page.
if (PAGE.state.busy) {
  const running = STAGES.find((s) => s.name === PAGE.state.stage);
  if (running) renderStage(running);
  banner(`${PAGE.state.stage} уже выполняется — подключаюсь к логу`);
  watch(PAGE.state.run_id);
}
