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
def client(tmp_path):
    # Imported here, not at module scope, for the same reason web/cli.py does:
    # this is the first point that genuinely needs Flask.
    from mpt_autopilot.web.app import create_app

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


def test_the_child_gets_an_absolute_config_path(tmp_path):
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
        return status_mod.MptStatus(True, True, 200, 7, api_url, "127.0.0.1:8080")

    monkeypatch.setattr(status_mod, "probe", fake_probe)
    body = app.test_client().get("/api/mpt-status").get_json()
    assert calls == ["http://127.0.0.1:8080"]
    assert body == {
        "configured": True,
        "up": True,
        "status_code": 200,
        "latency_ms": 7,
        "api_url": "http://127.0.0.1:8080",
        "host": "127.0.0.1:8080",
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


def test_the_configured_bind_host_is_allowed(tmp_path):
    from mpt_autopilot.web.app import create_app

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
