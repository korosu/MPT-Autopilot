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
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, render_template, request

from mpt_autopilot import config as shared_config
from mpt_autopilot.web import spec as spec_mod
from mpt_autopilot.web.argv import FormError, command_argv, flag_argv
from mpt_autopilot.web.runner import Runner, RunnerBusy, RunState


def _state_payload(state: RunState | None) -> dict[str, object]:
    if state is None:
        return {"busy": False, "run_id": None, "stage": None, "exit_code": None, "cancelled": False}
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


def create_app(
    cfg: shared_config.Config,
    config_path: Path | None = None,
    runner: Runner | None = None,
) -> Flask:
    app = Flask(__name__)
    runner = runner or Runner(cwd=cfg.dir)
    specs = {s.name: s for s in spec_mod.stage_specs() if s.name != "web"}
    page_spec = spec_mod.as_dict(list(specs.values()), cfg.langs())

    # Absolute, because the child runs with cwd=cfg.dir: a relative --config
    # would be re-resolved against the config's own directory and vanish.
    child_config = (config_path or cfg.path).resolve()

    @app.before_request
    def _same_origin_only():
        """
        Reject cross-site posts.

        There is no login here, so without this any page the user has open
        could POST to localhost and start an upload. A same-origin fetch sends
        no Origin on GET and its own origin on POST; anything else is refused.
        """
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return Response("cross-origin requests are not accepted", status=403)
        return None

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            config_path=str(cfg.path),
            page_spec=json.dumps(
                {**page_spec, "state": _state_payload(runner.state())}, ensure_ascii=False
            ),
        )

    @app.get("/api/status")
    def status() -> Response:
        return Response(
            json.dumps(_state_payload(runner.state()), ensure_ascii=False),
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
