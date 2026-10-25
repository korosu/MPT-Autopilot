"""
Tests for the `mpt web` subcommand and its Flask endpoints.

The AST check is the load-bearing one: cli.build_parser() imports web/cli.py on
every `mpt --help`, so a top-level `import flask` there would break the CLI for
everyone who skipped the optional extra. Checking sys.modules instead would
pass or fail depending on which test ran first.

The HTTP tests use Flask's test client with an injected Runner, so nothing here
starts a process or writes a file.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from mpt_autopilot import cli
from mpt_autopilot import config as shared_config
from mpt_autopilot.web import cli as web_cli
from mpt_autopilot.web.runner import Runner

# app.py imports Flask at module scope (it is only ever reached lazily), so the
# HTTP half of this file is skipped without the [web] extra. The AST checks
# below still run: they are what keep that extra optional.
pytest.importorskip("flask", reason="mpt web needs the [web] extra")


# ── CLI wiring ────────────────────────────────────────────────────────────────


def test_web_is_registered_as_a_stage():
    assert "web" in dict(cli.STAGES)
    hooks = cli._stage_hooks("web")
    assert callable(hooks.add_arguments) and callable(hooks.execute)
    assert "Examples:" in hooks.epilog


def test_web_help_needs_no_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # deliberately empty: no config.yaml here
    with pytest.raises(SystemExit) as exc:
        cli.main(["web", "--help"])
    assert exc.value.code == 0
    assert "--port" in capsys.readouterr().out


def test_web_defaults_to_loopback():
    args = web_cli.build_parser().parse_args([])
    assert (args.host, args.port, args.no_browser) == ("127.0.0.1", 8765, False)


def test_missing_config_fails_without_serving(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shared_config, "_cache", {})
    args = web_cli.build_parser().parse_args(["--no-browser"])
    assert web_cli.execute(args, tmp_path / "missing.yaml") == 1
    assert "[ERROR]" in capsys.readouterr().out


@pytest.mark.parametrize("module", ["__init__.py", "cli.py"])
def test_flask_is_never_imported_at_module_scope(module):
    """
    `mpt --help` imports both of these. Neither may pull in Flask.

    Parsed rather than executed: an import inside a function body is fine and
    is exactly how web/cli.py reaches the app.
    """
    source = Path(web_cli.__file__).with_name(module)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:  # top level only, by construction
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        assert not any(name.split(".")[0] == "flask" for name in names), (
            f"{module} imports flask at module scope"
        )


# ── HTTP endpoints ────────────────────────────────────────────────────────────


class FakeProc:
    def __init__(self, lines: list[str], code: int = 0):
        self.pid = 99
        self.returncode: int | None = None
        self._code = code
        self.stdout = iter(f"{line}\n" for line in lines)

    def wait(self) -> int:
        self.returncode = self._code
        return self._code

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:  # pragma: no cover - not reached in these tests
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Imported here, not at module scope, for the same reason web/cli.py does:
    # this is the first point that genuinely needs Flask.
    from mpt_autopilot.web import external as external_mod
    from mpt_autopilot.web.app import create_app

    # POST /api/run scans for terminal mpt runs before starting one; a real
    # mpt process on this machine must not turn the 202 tests flaky.
    monkeypatch.setattr(external_mod, "psutil_records", lambda: [])

    config = tmp_path / "config.yaml"
    config.write_text("langs:\n  en:\n    label: English\n", encoding="utf-8")
    cfg = shared_config.load(config, env_path=config, reload=True)
    runner = Runner(spawner=lambda argv: FakeProc(["hello"]))
    app = create_app(cfg, config, runner)
    app.config.update(TESTING=True)
    return app.test_client()


def test_the_page_carries_the_spec_and_the_langs(client):
    body = client.get("/").get_data(as_text=True)
    assert "page-spec" in body
    payload = json.loads(body.split('type="application/json">')[1].split("</script>")[0])
    assert {s["name"] for s in payload["stages"]} == {
        "refill",
        "init-seen",
        "batch",
        "enrich",
        "upload",
        "run",
        "validate",
    }
    assert payload["langs"] == [{"code": "en", "label": "English"}]
    assert payload["state"]["busy"] is False


def test_web_does_not_offer_to_launch_itself(client):
    """A `mpt web` button inside `mpt web` would just fork the server."""
    body = client.get("/").get_data(as_text=True)
    payload = json.loads(body.split('type="application/json">')[1].split("</script>")[0])
    assert "web" not in {s["name"] for s in payload["stages"]}


def test_the_page_offers_fill_from_config_and_reset(client):
    """Both pairs: the header's (any stage) and the form's (this stage)."""
    body = client.get("/").get_data(as_text=True)
    for button_id in ("fill-config", "reset-form", "fill-stage", "reset-stage"):
        assert f'id="{button_id}"' in body, button_id


def test_the_page_carries_the_config_defaults(tmp_path):
    """
    "Fill from config" reads flag.config_default, computed server-side from
    config.yaml — so the page has to ship the values, not the button.
    """
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text(
        "langs:\n"
        "  en:\n"
        "    label: English\n"
        "paths:\n"
        "  jobs_dir: ./jobs\n"
        "enricher:\n"
        "  platform: tiktok\n",
        encoding="utf-8",
    )
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)

    body = app.test_client().get("/").get_data(as_text=True)
    payload = json.loads(body.split('type="application/json">')[1].split("</script>")[0])
    flags = {s["name"]: {f["dest"]: f for f in s["flags"]} for s in payload["stages"]}

    assert flags["batch"]["jobs"]["config_default"] == str(tmp_path / "jobs" / "jobs.yaml")
    assert flags["enrich"]["platform"]["config_default"] == "tiktok"
    # A flag the config says nothing about stays unfilled.
    assert flags["batch"]["limit"]["config_default"] is None


def test_one_payload_carries_every_stages_defaults(tmp_path):
    """
    The header's "Fill from config" fills every tab at once, so a single page
    payload has to hold each stage's values — the button must not be limited to
    whatever the spec happened to build for the tab that is open.
    """
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text(
        "langs:\n"
        "  en:\n"
        "    label: English\n"
        "paths:\n"
        "  jobs_dir: ./jobs\n"
        "pilot:\n"
        "  generation:\n"
        "    count: 5\n"
        "enricher:\n"
        "  platform: tiktok\n",
        encoding="utf-8",
    )
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)

    body = app.test_client().get("/").get_data(as_text=True)
    payload = json.loads(body.split('type="application/json">')[1].split("</script>")[0])
    flags = {s["name"]: {f["dest"]: f for f in s["flags"]} for s in payload["stages"]}

    # Two stages that read different config sections, from one payload.
    assert flags["refill"]["count"]["config_default"] == "5"
    assert flags["enrich"]["platform"]["config_default"] == "tiktok"
    assert flags["run"]["refill_count"]["config_default"] == "5"
    # And every flag of every stage offers the key, filled or not.
    assert all("config_default" in f for s in payload["stages"] for f in s["flags"])


def test_an_invalid_form_is_rejected_before_anything_starts(client):
    response = client.post("/api/run", json={"stage": "refill", "flags": {}})
    assert response.status_code == 400
    assert "--lang" in response.get_json()["error"]
    assert client.get("/api/status").get_json()["busy"] is False


def test_an_unknown_stage_is_rejected(client):
    response = client.post("/api/run", json={"stage": "rm-rf", "flags": {}})
    assert response.status_code == 400


def test_a_run_streams_its_output_and_exit_code(client):
    started = client.post("/api/run", json={"stage": "validate", "flags": {}})
    assert started.status_code == 202
    body = started.get_json()
    # The echoed command is what the user would have typed: no interpreter
    # path, no absolute --config.
    assert body["command"] == ["validate"]

    stream = client.get(f"/api/stream/{body['run_id']}").get_data(as_text=True)
    assert 'event: line\ndata: "hello"' in stream
    assert "event: end" in stream
    assert '"exit_code": 0' in stream


def test_the_echoed_command_carries_the_flags(client):
    body = client.post("/api/run", json={"stage": "refill", "flags": {"lang": "en"}}).get_json()
    assert body["command"] == ["refill", "--lang", "en"]


def test_the_child_gets_an_absolute_config_path(tmp_path, monkeypatch):
    """
    The child runs with cwd set to the config's own directory, so a relative
    --config would resolve against that directory and disappear. This is the
    bug the manual run turned up: `.sandbox/config.yaml` became
    `.sandbox/.sandbox/config.yaml`.
    """
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text("langs:\n  en:\n    label: English\n", encoding="utf-8")
    cfg = shared_config.load(config, env_path=config, reload=True)

    seen: list[list[str]] = []

    def spy(argv: list[str]) -> FakeProc:
        seen.append(argv)
        return FakeProc([])

    from mpt_autopilot.web import external as external_mod

    monkeypatch.setattr(external_mod, "psutil_records", lambda: [])
    app = create_app(cfg, Path("config.yaml"), Runner(spawner=spy))
    app.config.update(TESTING=True)
    app.test_client().post("/api/run", json={"stage": "validate", "flags": {}})

    argv = seen[0]
    assert argv[1:3] == ["-m", "mpt_autopilot.cli"]
    assert Path(argv[argv.index("--config") + 1]).is_absolute()


def test_cancelling_nothing_is_a_conflict(client):
    assert client.post("/api/cancel").status_code == 409


def test_cross_origin_posts_are_refused(client):
    response = client.post(
        "/api/run",
        json={"stage": "validate", "flags": {}},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_same_origin_posts_are_allowed(client):
    response = client.post(
        "/api/run",
        json={"stage": "validate", "flags": {}},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 202


# ── MPT health endpoint ───────────────────────────────────────────────────────


def test_mpt_status_reports_not_configured_without_a_batch_section(client):
    body = client.get("/api/mpt-status").get_json()
    assert body["configured"] is False
    assert body["up"] is False


def test_mpt_status_probes_the_configured_api_url(tmp_path, monkeypatch):
    from mpt_autopilot.web import status as status_mod
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text(
        "langs:\n  en:\n    label: English\nbatch:\n  api_url: http://127.0.0.1:8080\n",
        encoding="utf-8",
    )
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)

    calls: list[str] = []

    def fake_probe(api_url, timeout=status_mod.PROBE_TIMEOUT_SECONDS, transport=None):
        calls.append(api_url)
        return status_mod.MptStatus(True, True, True, 200, 7, api_url, "127.0.0.1:8080", "tasks")

    monkeypatch.setattr(status_mod, "probe", fake_probe)
    body = app.test_client().get("/api/mpt-status").get_json()
    assert calls == ["http://127.0.0.1:8080"]
    assert body == {
        "configured": True,
        "up": True,
        "reachable": True,
        "status_code": 200,
        "latency_ms": 7,
        "api_url": "http://127.0.0.1:8080",
        "host": "127.0.0.1:8080",
        "via": "tasks",
    }


# ── external runs (started outside this page) ─────────────────────────────────


def _external_client(tmp_path: Path):
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text(
        "langs:\n  en:\n    label: English\nbatch:\n  log_file: ./logs/batch.log\n",
        encoding="utf-8",
    )
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)
    return app.test_client()


def test_external_endpoint_reports_a_terminal_run(tmp_path, monkeypatch):
    from mpt_autopilot.web import external as external_mod

    monkeypatch.setattr(
        external_mod,
        "psutil_records",
        lambda: [external_mod.ProcRecord(pid=1234, cmdline=["mpt", "batch"], create_time=100.0)],
    )
    monkeypatch.setattr(external_mod, "fill_cwd", lambda runs: runs)

    body = _external_client(tmp_path).get("/api/external").get_json()
    assert body["scanner"] == "psutil"
    assert body["runs"][0]["pid"] == 1234
    assert body["runs"][0]["stage"] == "batch"
    # The record names no config and cwd was stubbed away, so no log is shown.
    assert body["runs"][0]["logs"] == []


def test_external_endpoint_tails_the_log_of_a_matching_config(tmp_path, monkeypatch):
    from mpt_autopilot.web import external as external_mod

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "batch.log").write_text("working\n", encoding="utf-8")
    monkeypatch.setattr(
        external_mod,
        "psutil_records",
        lambda: [
            external_mod.ProcRecord(
                pid=1234,
                cmdline=["mpt", "batch", "--config", str(tmp_path / "config.yaml")],
                create_time=100.0,
            )
        ],
    )
    monkeypatch.setattr(external_mod, "fill_cwd", lambda runs: runs)

    body = _external_client(tmp_path).get("/api/external").get_json()
    assert [log["name"] for log in body["runs"][0]["logs"]] == ["batch"]
    assert body["runs"][0]["logs"][0]["tail"] == "working"


def test_external_endpoint_without_psutil_is_honest(tmp_path, monkeypatch):
    from mpt_autopilot.web import external as external_mod

    monkeypatch.setattr(external_mod, "psutil_records", lambda: None)
    assert _external_client(tmp_path).get("/api/external").get_json() == {
        "scanner": "unavailable",
        "runs": [],
    }


def test_a_terminal_run_blocks_starting_one_here(tmp_path, monkeypatch):
    """The run gate: a terminal mpt and a web mpt would fight over the same
    jobs.yaml / seen.txt / ledger, so POST /api/run refuses to race it."""
    from mpt_autopilot.web import external as external_mod

    monkeypatch.setattr(
        external_mod,
        "psutil_records",
        lambda: [external_mod.ProcRecord(pid=1234, cmdline=["mpt", "batch"], create_time=100.0)],
    )
    monkeypatch.setattr(external_mod, "fill_cwd", lambda runs: runs)

    response = _external_client(tmp_path).post("/api/run", json={"stage": "validate", "flags": {}})
    assert response.status_code == 409
    error = response.get_json()["error"]
    assert "mpt is already running outside this page" in error
    assert "PID 1234" in error


def test_the_run_gate_is_open_without_psutil(tmp_path, monkeypatch):
    """Without psutil the scanner sees nothing by design, so the gate degrades
    to open rather than locking the page on a scanner it does not have."""
    from mpt_autopilot.web import external as external_mod

    monkeypatch.setattr(external_mod, "psutil_records", lambda: None)
    response = _external_client(tmp_path).post("/api/run", json={"stage": "validate", "flags": {}})
    assert response.status_code == 202


# ── audit findings W-1, W-2, W-5 ──────────────────────────────────────────────


def test_dns_rebinding_hosts_are_refused(client):
    """W-1: an attacker's domain resolving to 127.0.0.1 sends a Host+Origin
    pair that matches itself — the origin comparison alone lets it through."""
    response = client.post(
        "/api/run",
        json={"stage": "validate", "flags": {}},
        headers={"Host": "evil.example:8765", "Origin": "http://evil.example:8765"},
    )
    assert response.status_code == 403


def test_the_configured_bind_host_is_allowed(tmp_path, monkeypatch):
    from mpt_autopilot.web import external as external_mod
    from mpt_autopilot.web.app import create_app

    monkeypatch.setattr(external_mod, "psutil_records", lambda: [])
    config = tmp_path / "config.yaml"
    config.write_text("langs:\n  en:\n    label: English\n", encoding="utf-8")
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])), host="mpt.local")
    app.config.update(TESTING=True)

    response = app.test_client().post(
        "/api/run",
        json={"stage": "validate", "flags": {}},
        headers={"Host": "mpt.local:9000"},
    )
    assert response.status_code == 202


def test_a_label_cannot_break_out_of_the_spec_script(tmp_path):
    """W-2: `| safe` let a `</script>` in any spec string close the tag and
    inject markup; `| tojson` escapes it instead."""
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text(
        "langs:\n  en:\n    label: '</script><script>alert(1)</script>'\n",
        encoding="utf-8",
    )
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)

    body = app.test_client().get("/").get_data(as_text=True)
    payload = body.split('type="application/json">')[1].split("</script>")[0]
    assert "<script>alert" not in payload
    assert json.loads(payload)["langs"][0]["label"] == "</script><script>alert(1)</script>"


def test_the_idle_status_payload_carries_started_at(client):
    """W-5: docs/web.md promised the key on every response, idle included."""
    body = client.get("/api/status").get_json()
    assert body["started_at"] is None


# ── the cancel lock sweep (problem 2) ────────────────────────────────────────


def test_lock_sweep_dirs_derive_from_the_config(tmp_path):
    """The sweep must look at the dirs the config actually uses — a stage
    override first, then the shared `paths:` fallback — and survive a
    malformed section instead of taking the page down."""
    from mpt_autopilot.web import app as app_mod

    config = tmp_path / "config.yaml"
    config.write_text(
        "paths:\n"
        "  seen_dir: ./seen\n"
        "  jobs_dir: ./jobs\n"
        "batch:\n"
        "  api_url: http://127.0.0.1:8080\n"
        "  seen_dir: ./batch_seen\n"
        "pilot: 5\n",  # a scalar section is malformed — skipped, not fatal
        encoding="utf-8",
    )
    cfg = shared_config.load(config, env_path=config, reload=True)

    dirs = app_mod._lock_sweep_dirs(cfg)

    # batch's own seen_dir wins over the shared one; jobs_dir falls back to
    # paths:, and the broken pilot section contributes nothing.
    assert dirs == [tmp_path / "batch_seen", tmp_path / "jobs"]


def test_lock_sweep_dirs_empty_for_a_bare_config(tmp_path):
    from mpt_autopilot.web import app as app_mod

    config = tmp_path / "config.yaml"
    config.write_text("langs:\n  en:\n    label: English\n", encoding="utf-8")
    cfg = shared_config.load(config, env_path=config, reload=True)
    assert app_mod._lock_sweep_dirs(cfg) == []


# ── the Settings page (problem 3) ───────────────────────────────────────────


def test_the_header_links_to_the_settings_page(client):
    assert 'href="/settings"' in client.get("/").get_data(as_text=True)


def test_the_settings_page_renders_without_commands(tmp_path):
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text("langs:\n  en:\n    label: English\n", encoding="utf-8")
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)

    body = app.test_client().get("/settings").get_data(as_text=True)
    assert "MoneyPrinterTurbo" in body
    # The two unconfigured cards must name the key that turns them on —
    # "not configured" without the how is a dead end.
    assert "mpt_restart_command" in body
    assert "mpt_logs_command" in body
    # The buttons only exist once the keys are configured.
    assert 'id="restart-mpt"' not in body
    assert 'id="logs-start"' not in body


def test_the_settings_page_shows_the_configured_commands(tmp_path):
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text(
        "web:\n"
        "  mpt_restart_command: 'sudo systemctl restart mpt'\n"
        '  mpt_logs_command: "journalctl -u mpt -f"\n',
        encoding="utf-8",
    )
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)

    body = app.test_client().get("/settings").get_data(as_text=True)
    # Autoescaped quotes make exact-match brittle; assert the payloads.
    assert "systemctl restart mpt" in body
    assert "journalctl -u mpt -f" in body
    assert 'id="restart-mpt"' in body
    assert 'id="logs-start"' in body


def _web_client(tmp_path, extra: str = ""):
    from mpt_autopilot.web.app import create_app

    config = tmp_path / "config.yaml"
    config.write_text("langs:\n  en:\n    label: English\n" + extra, encoding="utf-8")
    cfg = shared_config.load(config, env_path=config, reload=True)
    app = create_app(cfg, config, Runner(spawner=lambda argv: FakeProc(["x"])))
    app.config.update(TESTING=True)
    return app.test_client()


def test_details_joins_probe_version_and_deployment(client, monkeypatch):
    from mpt_autopilot.web import mpt_control as control_mod
    from mpt_autopilot.web import status as status_mod

    monkeypatch.setattr(
        status_mod,
        "probe",
        lambda url: status_mod.MptStatus(
            True, True, True, 200, 41, "http://127.0.0.1:8080", "127.0.0.1:8080", "tasks"
        ),
    )
    monkeypatch.setattr(control_mod, "deployment", lambda url: "local")
    monkeypatch.setattr(
        control_mod,
        "openapi_version",
        lambda url: {"version": "1.3.3", "title": "MoneyPrinterTurbo"},
    )

    body = client.get("/api/mpt/details").get_json()
    assert body["up"] is True
    assert body["via"] == "tasks"
    assert body["version"] == "1.3.3"
    assert body["deployment"] == "local"
    assert body["logs_live"] is False


def test_restart_and_logs_refuse_when_not_configured(client):
    restart = client.post("/api/mpt/restart")
    assert restart.status_code == 400
    assert "web.mpt_restart_command" in restart.get_json()["error"]

    start = client.post("/api/mpt/logs/start")
    assert start.status_code == 400
    assert "web.mpt_logs_command" in start.get_json()["error"]

    # And the stream endpoint has nothing to serve yet.
    assert client.get("/api/mpt/logs/stream").status_code == 404


def test_restart_runs_the_configured_command_and_reports_it(tmp_path, monkeypatch):
    from mpt_autopilot.web import mpt_control as control_mod

    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        control_mod,
        "run_restart",
        lambda command, cwd: (
            calls.append((command, cwd)) or {"ok": True, "exit_code": 0, "output": ["restarted"]}
        ),
    )
    client = _web_client(tmp_path, 'web:\n  mpt_restart_command: "systemctl restart mpt"\n')

    body = client.post("/api/mpt/restart").get_json()
    assert body == {"ok": True, "exit_code": 0, "output": ["restarted"]}
    # cfg.dir resolves against the config file, so compare resolved paths.
    assert calls == [("systemctl restart mpt", tmp_path.resolve())]


def test_the_log_stream_lifecycle_start_attach_stop(tmp_path, monkeypatch):
    from mpt_autopilot.web import mpt_control as control_mod

    class FakeCommandStream:
        def __init__(self, command: str, cwd: Path):
            self.command = command
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def is_live(self) -> bool:
            return self.started and not self.stopped

        def stop(self) -> bool:
            if self.is_live():
                self.stopped = True
                return True
            return False

        def subscribe(self, last_id: int = 0):
            yield (1, "log line one")
            yield (2, "log line two")

    streams: list[FakeCommandStream] = []
    monkeypatch.setattr(
        control_mod,
        "CommandStream",
        lambda command, cwd: streams.append(FakeCommandStream(command, cwd)) or streams[-1],
    )
    client = _web_client(tmp_path, 'web:\n  mpt_logs_command: "journalctl -u mpt -f"\n')

    assert client.post("/api/mpt/logs/start").status_code == 202
    assert streams[-1].command == "journalctl -u mpt -f"
    assert streams[-1].started

    # A second Start is refused — one follow-command at a time.
    assert client.post("/api/mpt/logs/start").status_code == 409

    body = client.get("/api/mpt/logs/stream").get_data(as_text=True)
    assert 'data: "log line one"' in body
    assert 'data: "log line two"' in body
    assert "event: end" in body

    # The details endpoint tells a reloaded page that a stream is live.
    assert client.get("/api/mpt/details").get_json()["logs_live"] is True

    assert client.post("/api/mpt/logs/stop").status_code == 202
    assert client.post("/api/mpt/logs/stop").status_code == 409  # already dead


# ── the setup wizard: pick how MPT runs, commands derive ─────────────────────


def test_setup_get_prefills_the_saved_choice(tmp_path):
    client = _web_client(tmp_path)
    body = client.get("/api/mpt/setup").get_json()
    assert body["deployment"] is None
    assert body["restart"] is None
    assert body["logs"] is None
    assert body["logs_kind"] == "none"

    # Save a choice through the wizard's own endpoint, then read it back.
    saved = client.post("/api/mpt/setup", json={"type": "docker", "name": "moneyprinter"})
    assert saved.status_code == 200
    body = saved.get_json()
    assert body["deployment"] == {"type": "docker", "name": "moneyprinter"}
    assert body["restart"] == "docker restart moneyprinter"
    assert body["logs"] == "docker logs -f moneyprinter"
    assert body["logs_kind"] == "command"

    # And GET now serves the same view.
    assert client.get("/api/mpt/setup").get_json() == body


def test_setup_post_validates_and_rejects_injections(tmp_path):
    client = _web_client(tmp_path)
    for bad in (
        {},  # no type
        {"type": "wat", "name": "x"},
        {"type": "systemd"},  # no name
        {"type": "tmux", "name": "x"},  # tmux without a start command
        {"type": "systemd", "name": "mpt\nrm -rf /"},  # the one injection
    ):
        answer = client.post("/api/mpt/setup", json=bad)
        assert answer.status_code == 400, bad
        assert answer.get_json()["error"]
    # Nothing was saved by a rejected post.
    assert client.get("/api/mpt/setup").get_json()["deployment"] is None


def test_wizard_commands_drive_the_buttons_and_tmux_logs_poll(tmp_path, monkeypatch):
    from mpt_autopilot.web import mpt_control as control_mod

    client = _web_client(tmp_path)
    saved = client.post(
        "/api/mpt/setup", json={"type": "tmux", "name": "mpt", "start_command": "python app.py"}
    )
    assert saved.status_code == 200

    # Restart runs the wizard-derived line, no config.yaml key involved.
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        control_mod,
        "run_restart",
        lambda command, cwd: (
            calls.append((command, cwd)) or {"ok": True, "exit_code": 0, "output": []}
        ),
    )
    body = client.post("/api/mpt/restart").get_json()
    assert body["ok"] is True
    assert calls == [("tmux kill-session -t mpt && python app.py", tmp_path.resolve())]

    # tmux logs are a poller, not a command.
    class FakePoller:
        def __init__(self, session: str):
            self.session = session
            self.started = False

        def start(self) -> None:
            self.started = True

        def is_live(self) -> bool:
            return self.started

        def stop(self) -> bool:
            return self.started

        def subscribe(self, last_id: int = 0):
            yield (1, "pane line")

    pollers: list[FakePoller] = []
    monkeypatch.setattr(
        control_mod,
        "TmuxPollStream",
        lambda session: pollers.append(FakePoller(session)) or pollers[-1],
    )
    assert client.post("/api/mpt/logs/start").status_code == 202
    assert pollers[-1].session == "mpt"
    assert pollers[-1].started
    body = client.get("/api/mpt/logs/stream").get_data(as_text=True)
    assert 'data: "pane line"' in body


def test_a_manual_key_beats_the_wizard_through_the_api(tmp_path):
    client = _web_client(tmp_path, 'web:\n  mpt_logs_command: "tail -f mpt.log"\n')
    assert client.post("/api/mpt/setup", json={"type": "docker", "name": "mpt"}).status_code == 200
    body = client.get("/api/mpt/setup").get_json()
    # The manual logs key wins; the wizard only names the container.
    assert body["logs"] == "tail -f mpt.log"
    assert body["restart"] == "docker restart mpt"


def test_the_settings_page_shows_the_wizard_with_the_saved_choice(tmp_path):
    client = _web_client(tmp_path)
    assert (
        client.post(
            "/api/mpt/setup", json={"type": "tmux", "name": "mpt", "start_command": "python app.py"}
        ).status_code
        == 200
    )
    body = client.get("/settings").get_data(as_text=True)
    # The form is prefilled with the saved choice…
    assert 'value="mpt"' in body
    assert 'value="python app.py"' in body
    # …the ssh row is gone entirely…
    assert "setup-host" not in body
    assert "SSH" not in body
    # …and the derived commands light the buttons up without any config.yaml
    # key ever being touched.
    assert 'id="restart-mpt"' in body
    assert 'id="logs-start"' in body
