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
