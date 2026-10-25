# pyright: reportMissingImports=false
# Flask is an optional extra (`uv sync --extra web`): type checks may run in
# an environment that does not have it (a plain `uv sync --extra dev`), and
# flask is the only import such an environment cannot resolve. Per-file rule
# override, not a blanket `# type: ignore` — everything else stays checked.
# The directive must precede any statement, including the module docstring.
"""
web/app.py — the Flask layer: one page, four endpoints, no state of its own.

Everything real lives in spec/argv/runner; this file only translates HTTP into
those calls. It is imported lazily by web/cli.py, which is why it may import
Flask at module scope.

Log delivery is Server-Sent Events, not a websocket: EventSource is built into
every browser, reconnects by itself, and resends Last-Event-ID so a reload
resumes mid-run instead of starting from a blank pane.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, render_template, request

from mpt_autopilot import config as shared_config
from mpt_autopilot.web import config_defaults, mpt_control
from mpt_autopilot.web import external as external_mod
from mpt_autopilot.web import spec as spec_mod
from mpt_autopilot.web import status as status_mod
from mpt_autopilot.web.argv import FormError, command_argv, flag_argv
from mpt_autopilot.web.cli import DEFAULT_HOST
from mpt_autopilot.web.runner import Runner, RunnerBusy, RunState


def _state_payload(state: RunState | None) -> dict[str, object]:
    if state is None:
        return {
            "busy": False,
            "run_id": None,
            "stage": None,
            "started_at": None,
            "exit_code": None,
            "cancelled": False,
        }
    return {
        "busy": not state.finished,
        "run_id": state.run_id,
        "stage": state.stage,
        "started_at": state.started_at,
        "exit_code": state.exit_code,
        "cancelled": state.cancelled,
    }


def _sse(event: str, data: object, line_id: int | None = None) -> str:
    head = f"id: {line_id}\n" if line_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _last_event_id(raw: str | None) -> int:
    try:
        return max(0, int(raw or 0))
    except ValueError:
        return 0


def _lock_sweep_dirs(cfg: shared_config.Config) -> list[Path]:
    """
    Directories whose `*.lock` markers mpt web may sweep after killing a run.

    Stage locks sit next to their target files: `<seen stem>.run.lock` and
    `seen.txt.lock` live in the seen dir, jobs locks in the jobs dir. Both
    stage sections are consulted because a config may point pilot at a
    different seen_dir than batch; each path_value call falls back to the
    shared `paths:` block on its own. The sweep only removes markers whose
    recorded holder is the child it just killed, so a wider net cannot touch
    a live run's lock.
    """
    dirs: list[Path] = []
    for section, key in (
        ("pilot", "seen_dir"),
        ("pilot", "jobs_dir"),
        ("batch", "seen_dir"),
        ("batch", "jobs_dir"),
    ):
        try:
            value = cfg.path_value(section, key)
        # A malformed section (a scalar `pilot:`) must not take the page down
        # with it — the sweep is an optional convenience, not a dependency.
        except (shared_config.ConfigError, OSError):
            continue
        if value is not None:
            dirs.append(Path(value))
    # Keep the first occurrence of each directory, in discovery order.
    return list(dict.fromkeys(dirs))


def create_app(
    cfg: shared_config.Config,
    config_path: Path | None = None,
    runner: Runner | None = None,
    host: str = DEFAULT_HOST,
) -> Flask:
    app = Flask(__name__)
    runner = runner or Runner(cwd=cfg.dir, lock_dirs=_lock_sweep_dirs(cfg))
    specs = {s.name: s for s in spec_mod.stage_specs() if s.name != "web"}
    # config_defaults answers "what would this config.yaml actually use" for
    # the fields the form can fill from it; see web/config_defaults.py.
    page_spec = spec_mod.as_dict(
        list(specs.values()), cfg.langs(), config_defaults.stage_defaults(cfg)
    )

    # Absolute, because the child runs with cwd=cfg.dir: a relative --config
    # would be re-resolved against the config's own directory and vanish.
    child_config = (config_path or cfg.path).resolve()

    # The MPT health dot probes the same server a real render would use; a
    # missing `batch:` section means the dot reports "not configured".
    mpt_api_url = str(cfg.section("batch").get("api_url") or "") or None

    # W-1: the Hosts this server answers. Loopback in every spelling, plus
    # whatever --host was bound to. Without the check, DNS rebinding — an
    # attacker's domain resolving to 127.0.0.1 — sends a Host+Origin pair
    # that matches itself and slips past the origin comparison below.
    allowed_hosts = {"127.0.0.1", "localhost", "::1", host.lower()}

    @app.before_request
    def _same_origin_only():
        """
        Reject cross-site posts and rebound hosts.

        There is no login here, so without this any page the user has open
        could POST to localhost and start an upload. A same-origin fetch sends
        no Origin on GET and its own origin on POST; anything else is refused.
        """
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return Response("cross-origin requests are not accepted", status=403)
        # request.host carries the port; urlsplit strips it and unwraps IPv6.
        hostname = (urlsplit(f"//{request.host}").hostname or "").lower()
        if hostname not in allowed_hosts:
            return Response("requests to an unexpected host are refused", status=403)
        return None

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            config_path=str(cfg.path),
            # | tojson in the template escapes <, >, & and ', so no spec
            # string (argparse help, a config label) can close the script
            # tag and inject markup (W-2).
            page_spec={**page_spec, "state": _state_payload(runner.state())},
        )

    @app.get("/api/status")
    def status() -> Response:
        return Response(
            json.dumps(_state_payload(runner.state()), ensure_ascii=False),
            mimetype="application/json",
        )

    @app.get("/api/mpt-status")
    def mpt_status() -> Response:
        # One probe per request on purpose: one user polls every ~15 s, so a
        # cache would only make the dot lag behind reality.
        return Response(
            json.dumps(status_mod.probe(mpt_api_url).as_dict(), ensure_ascii=False),
            mimetype="application/json",
        )

    # ── Settings page: MPT facts + the two narrow commands ─────────────────
    # Restart and logs run ONLY what config.yaml spelled out
    # (web.mpt_restart_command / web.mpt_logs_command); see mpt_control.py
    # for why nothing about the deployment is hardcoded.

    # The one logs stream the app may hold. Replaced — not reused — by every
    # Start: a finished journalctl follow is a dead pipe, and "start" means a
    # fresh command. TmuxPollStream rides the same slot: a pane poller and a
    # follow command answer the same subscribe contract.
    logs_stream: mpt_control.CommandStream | mpt_control.TmuxPollStream | None = None

    @app.get("/settings")
    def settings_page() -> str:
        resolved = mpt_control.resolve_commands(cfg)
        return render_template(
            "settings.html",
            config_path=str(cfg.path),
            restart_cmd=resolved.restart,
            # Which sentence the card shows: "from web.mpt_restart_command"
            # vs "derived from your wizard answer".
            restart_manual=mpt_control.restart_command(cfg) is not None,
            logs_cmd=resolved.logs,
            logs_manual=mpt_control.logs_command(cfg) is not None,
            logs_kind=resolved.logs_kind,
            deployment=resolved.deployment,
        )

    @app.get("/api/mpt/details")
    def mpt_details() -> Response:
        status = status_mod.probe(mpt_api_url)
        payload: dict[str, object] = {
            **status.as_dict(),
            "deployment": mpt_control.deployment(mpt_api_url),
            "version": None,
            "title": None,
            # A page that reloads mid-stream re-attaches instead of starting
            # a second copy of the command.
            "logs_live": logs_stream is not None and logs_stream.is_live(),
        }
        # Only an answering server can have an OpenAPI document — asking a
        # dead port would just double the button's wait.
        if status.reachable:
            payload.update(mpt_control.openapi_version(mpt_api_url) or {})
        return Response(json.dumps(payload, ensure_ascii=False), mimetype="application/json")

    # ── the setup wizard: pick how MPT runs, commands derive ──────────────

    def _setup_view(resolved: mpt_control.ResolvedMptCommands) -> dict[str, object]:
        """The wizard form's whole world: the saved choice plus the commands
        it resolves to (manual config.yaml keys included), so the UI can
        show what Save did without a second round trip."""
        dep = resolved.deployment
        choice: dict[str, str] | None = None
        if dep is not None:
            choice = {"type": dep.type, "name": dep.name}
            if dep.start_command is not None:
                choice["start_command"] = dep.start_command
        return {
            "deployment": choice,
            "restart": resolved.restart,
            "logs": resolved.logs,
            "logs_kind": resolved.logs_kind,
        }

    @app.get("/api/mpt/setup")
    def mpt_setup_get() -> Response:
        return Response(
            json.dumps(_setup_view(mpt_control.resolve_commands(cfg)), ensure_ascii=False),
            mimetype="application/json",
        )

    @app.post("/api/mpt/setup")
    def mpt_setup_post() -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("the setup must be a JSON object", 400)
        try:
            deployment = mpt_control.validate_deployment(payload)
        except ValueError as exc:
            return _error(str(exc), 400)
        mpt_control.save_deployment(cfg, deployment)
        return Response(
            json.dumps(_setup_view(mpt_control.resolve_commands(cfg)), ensure_ascii=False),
            mimetype="application/json",
        )

    @app.post("/api/mpt/restart")
    def mpt_restart() -> Response:
        command = mpt_control.resolve_commands(cfg).restart
        if command is None:
            return _error(
                "web.mpt_restart_command is not set in config.yaml and the "
                "setup wizard has no saved deployment — set one of the two",
                400,
            )
        result = mpt_control.run_restart(command, cfg.dir)
        return Response(json.dumps(result, ensure_ascii=False), mimetype="application/json")

    @app.post("/api/mpt/logs/start")
    def mpt_logs_start() -> Response:
        nonlocal logs_stream
        resolved = mpt_control.resolve_commands(cfg)
        command = resolved.logs
        # kind "tmux" implies a saved deployment (resolve_commands' shape);
        # the and-clause keeps pyright's narrow without an assert.
        session = (
            resolved.deployment.name
            if resolved.logs_kind == "tmux" and resolved.deployment is not None
            else None
        )
        if logs_stream is not None and logs_stream.is_live():
            return _error("the MPT log stream is already running", 409)
        if session is not None:
            # A pane has no follow mode; the poller answers the same
            # subscribe contract the SSE route already speaks.
            stream = mpt_control.TmuxPollStream(session)
            stream.start()
        elif command is not None:
            stream = mpt_control.CommandStream(command, cfg.dir)
            try:
                stream.start()
            except OSError as exc:
                return _error(f"could not start the log command: {exc}", 500)
        else:
            return _error(
                "web.mpt_logs_command is not set in config.yaml and the "
                "setup wizard has no saved deployment — set one of the two",
                400,
            )
        logs_stream = stream
        return Response(status=202)

    @app.post("/api/mpt/logs/stop")
    def mpt_logs_stop() -> Response:
        if logs_stream is None or not logs_stream.stop():
            return _error("the log stream is not running", 409)
        return Response(status=202)

    @app.get("/api/mpt/logs/stream")
    def mpt_logs_stream() -> Response:
        if logs_stream is None:
            return _error("no log stream has been started yet", 404)
        # Read before building the generator: outside the request context
        # there are no headers to read. The stream is captured because a
        # Start racing this response replaces the app-level reference.
        last_id = _last_event_id(request.headers.get("Last-Event-ID"))
        stream = logs_stream

        def events() -> Iterator[str]:
            for item in stream.subscribe(last_id):
                if item is None:
                    yield ": keepalive\n\n"
                else:
                    yield _sse("line", item[1], line_id=item[0])
            yield _sse("end", {})

        return Response(
            events(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # in case anyone proxies this
            },
        )

    def _scan_external() -> list[external_mod.ExternalRun] | None:
        """Runs started outside this page, or None when psutil is missing.

        Shared by the read-only list and the run gate below: both must
        exclude this server's own child, or the page would list (and then
        refuse to restart) the very run it is streaming.
        """
        records = external_mod.psutil_records()
        if records is None:
            return None
        state = runner.state()
        exclude: set[int] = set()
        if state is not None and not state.finished and state.pid is not None:
            exclude.add(state.pid)
        return external_mod.fill_cwd(
            external_mod.scan(records, exclude_pids=exclude, self_pid=os.getpid())
        )

    @app.get("/api/external")
    def external() -> Response:
        # Read-only view of mpt runs started outside this page (a terminal, a
        # cron job): which stage runs, its PID, and the tail of that stage's
        # file log when the run provably uses this page's own config.
        runs = _scan_external()
        if runs is None:
            return Response(
                json.dumps({"scanner": "unavailable", "runs": []}, ensure_ascii=False),
                mimetype="application/json",
            )
        return Response(
            json.dumps(
                {
                    "scanner": "psutil",
                    "runs": [external_mod.run_payload(run, cfg, child_config) for run in runs],
                },
                ensure_ascii=False,
            ),
            mimetype="application/json",
        )

    @app.post("/api/run")
    def run() -> Response:
        body = request.get_json(silent=True) or {}
        stage = str(body.get("stage", ""))
        flags = body.get("flags") or {}
        if stage not in specs:
            return _error(f"unknown stage {stage!r}", 400)
        if not isinstance(flags, dict):
            return _error("flags must be an object", 400)

        try:
            flag_args = flag_argv(specs[stage], flags)
        except FormError as exc:
            return _error(str(exc), 400)
        argv = command_argv(stage, flag_args, child_config)

        # A terminal run and a web run would fight over the same jobs.yaml,
        # seen.txt and ledgers. The terminal wins — it was here first — so
        # the page refuses to start instead of racing it.
        external_runs = _scan_external()
        if external_runs:
            first = external_runs[0]
            return _error(
                f"mpt is already running outside this page: {first.command} "
                f"(PID {first.pid}, stage {first.stage})",
                409,
            )

        try:
            state = runner.start(stage, argv)
        except RunnerBusy as exc:
            return _error(str(exc), 409)
        except OSError as exc:
            # A missing interpreter or an exhausted process table: a real
            # failure, but the user's problem to fix, not a traceback.
            return _error(f"could not start {stage}: {exc}", 500)

        return Response(
            # The command as the user would have typed it: the absolute
            # --config the child really gets is noise on the page.
            json.dumps(
                {"run_id": state.run_id, "command": [stage, *flag_args]}, ensure_ascii=False
            ),
            status=202,
            mimetype="application/json",
        )

    @app.post("/api/cancel")
    def cancel() -> Response:
        if not runner.cancel():
            return _error("nothing is running", 409)
        return Response(status=202)

    @app.get("/api/stream/<int:run_id>")
    def stream(run_id: int) -> Response:
        # Read before building the generator: outside the request context
        # there are no headers to read.
        last_id = _last_event_id(request.headers.get("Last-Event-ID"))

        def events() -> Iterator[str]:
            for item in runner.subscribe(run_id, last_id):
                if item is None:
                    yield ": keepalive\n\n"
                else:
                    yield _sse("line", item[1], line_id=item[0])
            yield _sse("end", _state_payload(runner.state()))

        return Response(
            events(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # in case anyone proxies this
            },
        )

    return app


def _error(message: str, status: int) -> Response:
    return Response(
        json.dumps({"error": message}, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )
