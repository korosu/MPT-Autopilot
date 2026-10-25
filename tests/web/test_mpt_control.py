"""mpt_control.py — the Settings page's facts and its two narrow commands.

The narrow-exec rule is the point of the module, so it is the point of the
tests too: the page may only run what config.yaml spelled out, and every
helper degrades to "unknown"/None rather than guessing.
"""

from __future__ import annotations

import io
import itertools
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx
import pytest

from mpt_autopilot import config as shared_config
from mpt_autopilot.web import mpt_control


def _cfg(tmp_path: Path, text: str) -> shared_config.Config:
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    return shared_config.load(config, env_path=config, reload=True)


# ── config readers ───────────────────────────────────────────────────────────


def test_commands_come_from_the_web_section(tmp_path):
    cfg = _cfg(
        tmp_path,
        "web:\n"
        "  mpt_restart_command: 'sudo systemctl restart mpt'\n"
        '  mpt_logs_command: "journalctl -u mpt -f"\n',
    )
    assert mpt_control.restart_command(cfg) == "sudo systemctl restart mpt"
    assert mpt_control.logs_command(cfg) == "journalctl -u mpt -f"


def test_missing_keys_and_sections_read_as_not_configured(tmp_path):
    assert mpt_control.restart_command(_cfg(tmp_path, "langs:\n  en:\n    label: E\n")) is None
    assert mpt_control.logs_command(_cfg(tmp_path, "langs:\n  en:\n    label: E\n")) is None
    # A scalar `web:` section is malformed — tolerated, not fatal.
    scalar = _cfg(tmp_path, "web: 5\n")
    assert mpt_control.restart_command(scalar) is None
    # A non-string value is "not configured" too, never "run this list".
    listed = _cfg(tmp_path, "web:\n  mpt_restart_command: [a, b]\n")
    assert mpt_control.restart_command(listed) is None
    # Blank strings count as absent: the buttons stay disabled.
    blank = _cfg(tmp_path, 'web:\n  mpt_logs_command: "   "\n')
    assert mpt_control.logs_command(blank) is None


# ── openapi_version ──────────────────────────────────────────────────────────


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_openapi_version_reads_info_from_a_live_mpt():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi.json"
        return httpx.Response(
            200, json={"info": {"version": "1.3.3", "title": "MoneyPrinterTurbo"}}
        )

    found = mpt_control.openapi_version("http://127.0.0.1:8080", transport=_transport(handler))
    assert found == {"version": "1.3.3", "title": "MoneyPrinterTurbo"}


def test_openapi_version_is_none_when_the_port_does_not_answer_like_fastapi():
    for body, status in ((b"not json", 200), (b'{"info": "x"}', 200), (b"{}", 404)):

        def handler(request: httpx.Request, body=body, status=status) -> httpx.Response:
            return httpx.Response(status, content=body)

        assert (
            mpt_control.openapi_version("http://127.0.0.1:8080", transport=_transport(handler))
            is None
        )


def test_openapi_version_is_none_without_a_url_or_when_transport_dies():
    assert mpt_control.openapi_version(None) is None

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert (
        mpt_control.openapi_version("http://127.0.0.1:8080", transport=_transport(handler)) is None
    )


# ── deployment ───────────────────────────────────────────────────────────────


def test_deployment_names_the_port_owner():
    holder = lambda port: {"pid": 7, "name": "python.exe"}  # noqa: E731
    assert mpt_control.deployment("http://127.0.0.1:8080", port_holder=holder) == "local"
    holder = lambda port: {"pid": 7, "name": "docker-proxy"}  # noqa: E731
    assert (
        mpt_control.deployment("http://127.0.0.1:8080", port_holder=holder) == "other: docker-proxy"
    )
    holder = lambda port: {"pid": None, "name": ""}  # noqa: E731
    assert mpt_control.deployment("http://127.0.0.1:8080", port_holder=holder) == "unknown"


def test_deployment_when_nothing_listens_or_scanning_fails():
    assert (
        mpt_control.deployment("http://127.0.0.1:8080", port_holder=lambda port: None)
        == "port-free"
    )

    def no_psutil(port: int):
        raise ImportError("psutil")

    assert mpt_control.deployment("http://127.0.0.1:8080", port_holder=no_psutil) == "unknown"


def test_deployment_does_not_pretend_to_scan_a_remote_host():
    assert mpt_control.deployment("http://192.0.2.10:8080") == "remote-host"
    assert mpt_control.deployment(None) == "unconfigured"
    # No port in the URL means there is nothing to look up.
    assert mpt_control.deployment("http://127.0.0.1") == "unknown"


def test_psutil_port_holder_finds_the_listener(monkeypatch):
    psutil = pytest.importorskip("psutil")

    class Conn:
        def __init__(self, status, laddr, pid):
            self.status = status
            self.laddr = laddr
            self.pid = pid

    class Addr:
        def __init__(self, port):
            self.port = port

    conns = [
        Conn("ESTABLISHED", Addr(54321), 11),  # an outbound socket: ignored
        Conn(psutil.CONN_LISTEN, Addr(8080), 27928),
    ]
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": conns)
    monkeypatch.setattr(
        psutil, "Process", lambda pid: type("P", (), {"name": lambda self: "python.exe"})()
    )

    holder = mpt_control.psutil_port_holder(8080)
    assert holder == {"pid": 27928, "name": "python.exe"}
    assert mpt_control.psutil_port_holder(9999) is None


# ── run_restart ──────────────────────────────────────────────────────────────

# Real subprocess, but only of our own interpreter: the command string the
# user would have written is exercised end-to-end (shell=True included),
# without depending on echo/exit spelling differences between cmd and sh.
_PY = f'"{sys.executable}" -c'


def test_run_restart_reports_output_and_success(tmp_path):
    result = mpt_control.run_restart(f"{_PY} \"print('restarted')\"", tmp_path)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["output"] == ["restarted"]


def test_run_restart_reports_a_nonzero_exit(tmp_path):
    result = mpt_control.run_restart(f"{_PY} \"import sys; print('boom'); sys.exit(3)\"", tmp_path)
    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert result["output"] == ["boom"]


def test_run_restart_times_out_instead_of_hanging(tmp_path):
    result = mpt_control.run_restart(f'{_PY} "import time; time.sleep(30)"', tmp_path, timeout=0.5)
    assert result["ok"] is False
    assert result["output"] == ["[web] restart command timed out after 0.5s"]


# ── CommandStream ────────────────────────────────────────────────────────────


class FakeProc:
    """A finished child whose stdout is already fully readable."""

    def __init__(self, text: str, pid: int = 4242):
        self.pid = pid
        # Typed as the ChildStream protocol sees it, not as StringIO, so a
        # test can swap in a blocking stand-in pipe.
        self.stdout: Iterable[str] | None = io.StringIO(text)
        self._poll = None

    def wait(self) -> int:
        return 0

    def poll(self):
        return self._poll

    def terminate(self) -> None:  # pragma: no cover - kill_tree is patched
        pass


def _stream(tmp_path: Path, text: str) -> mpt_control.CommandStream:
    return mpt_control.CommandStream(
        "fake logs command",
        tmp_path,
        spawner=lambda command: FakeProc(text),
    )


def _collect(
    stream: mpt_control.CommandStream | mpt_control.TmuxPollStream, last_id: int = 0
) -> list[tuple[int, str]]:
    return [item for item in stream.subscribe(last_id) if item is not None]


def test_the_stream_yields_every_line_then_ends(tmp_path):
    stream = _stream(tmp_path, "line one\nline two\n")
    stream.start()
    assert _collect(stream) == [(1, "line one"), (2, "line two")]
    assert not stream.is_live()


def test_a_late_subscriber_replays_from_the_beginning(tmp_path):
    stream = _stream(tmp_path, "old\nnew\n")
    stream.start()
    assert _collect(stream, last_id=1) == [(2, "new")]
    # The next subscriber joins after the command ended and still replays:
    # journalctl's recent lines are the reason someone opens the pane.
    assert _collect(stream) == [(1, "old"), (2, "new")]


def test_stop_kills_the_child_and_the_stream_ends(tmp_path, monkeypatch):
    killed: list[object] = []
    monkeypatch.setattr(mpt_control, "kill_tree", lambda proc: killed.append(proc))

    class BlockingStdout:
        """Yields nothing until closed — the stand-in for a pipe journalctl
        -f keeps open. Iteration ends only when the kill closes it."""

        def __init__(self) -> None:
            self._closed = threading.Event()

        def __iter__(self) -> Iterator[str]:
            # Blocks inside iter(), like a real pipe with no data yet.
            while not self._closed.is_set():
                self._closed.wait(timeout=0.05)
            return iter(())

    proc = FakeProc("", pid=4242)
    blocker = BlockingStdout()
    proc.stdout = blocker
    stream = mpt_control.CommandStream("endless", tmp_path, spawner=lambda command: proc)
    stream.start()
    assert stream.is_live()
    assert stream.stop() is True
    assert killed == [proc]

    # What kill_tree would have done to the real pipe: end it, so the pump
    # can finish and the subscriber below observes the stream closing.
    blocker._closed.set()
    assert _collect(stream) == []
    assert not stream.is_live()
    # Only once the pump has noticed the death does stop() say "nothing to
    # stop" — a second call racing the pump may re-signal the kill, which is
    # harmless (Runner.cancel has the same property on a dying child).
    assert stream.stop() is False


def test_a_second_start_on_the_same_stream_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(mpt_control, "kill_tree", lambda proc: None)
    stream = _stream(tmp_path, "x\n")
    stream.start()
    with pytest.raises(RuntimeError):
        stream.start()


# ── the setup wizard: web-settings.yaml + derived commands ───────────────────


def test_validate_deployment_accepts_each_type_with_its_fields():
    assert mpt_control.validate_deployment({"type": "systemd", "name": "moneyprinter"}) == (
        mpt_control.MptDeployment(type="systemd", name="moneyprinter")
    )
    assert mpt_control.validate_deployment(
        {"type": "docker", "name": "mpt"}
    ) == mpt_control.MptDeployment(type="docker", name="mpt")
    # tmux needs the start command: restart = kill session + start it again.
    assert mpt_control.validate_deployment(
        {"type": "tmux", "name": "mpt", "start_command": "cd mpt && python app.py"}
    ) == mpt_control.MptDeployment(type="tmux", name="mpt", start_command="cd mpt && python app.py")


def test_validate_deployment_rejects_missing_or_multiline_fields():
    for bad, why in (
        ({"type": "wat", "name": "x"}, "unknown deployment type"),
        ({"name": "x"}, "type is required"),
        ({"type": "systemd"}, "name is required"),
        ({"type": "systemd", "name": "   "}, "blank name"),
        ({"type": "tmux", "name": "x"}, "tmux needs a start command"),
        ({"type": "systemd", "name": "mpt\nrm -rf /"}, "multiline name"),
        ({"type": "tmux", "name": "x", "start_command": "a\nb"}, "multiline start command"),
    ):
        with pytest.raises(ValueError):
            mpt_control.validate_deployment(bad)


def test_derived_commands_match_the_deployment_type():
    dep = mpt_control.MptDeployment(type="systemd", name="moneyprinter")
    assert mpt_control.derived_restart(dep) == "sudo systemctl restart moneyprinter"
    # No sudo on logs: without a TTY sudo dies on the password prompt and
    # would kill the stream for users whose journal group already can read.
    assert mpt_control.derived_logs(dep) == "journalctl -u moneyprinter -f"

    docker = mpt_control.MptDeployment(type="docker", name="mpt")
    assert mpt_control.derived_restart(docker) == "docker restart mpt"
    assert mpt_control.derived_logs(docker) == "docker logs -f mpt"

    tmux = mpt_control.MptDeployment(type="tmux", name="mpt", start_command="python app.py")
    assert mpt_control.derived_restart(tmux) == "tmux kill-session -t mpt && python app.py"
    # tmux has no follow mode: the page polls capture-pane instead of a shell
    # command — derived_logs says so with None.
    assert mpt_control.derived_logs(tmux) is None


def test_web_settings_round_trip_and_tolerant_load(tmp_path):
    cfg = _cfg(tmp_path, "langs:\n  en:\n    label: E\n")

    # No file yet — nothing configured, never an error.
    assert mpt_control.load_deployment(cfg) is None

    dep = mpt_control.MptDeployment(type="docker", name="mpt")
    mpt_control.save_deployment(cfg, dep)
    assert mpt_control.load_deployment(cfg) == dep

    # Atomic write: no temp leftovers next to the settings file.
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    (tmp_path / "web-settings.yaml").write_text("deployment: 5\n", encoding="utf-8")
    assert mpt_control.load_deployment(cfg) is None
    (tmp_path / "web-settings.yaml").write_text(":::\n", encoding="utf-8")
    assert mpt_control.load_deployment(cfg) is None
    # A saved-but-incomplete choice (tmux without its start command) loads
    # as nothing — save only happens after validate, so this is defence
    # in depth.
    (tmp_path / "web-settings.yaml").write_text(
        "deployment:\n  type: tmux\n  name: mpt\n", encoding="utf-8"
    )
    assert mpt_control.load_deployment(cfg) is None


def test_resolve_commands_manual_keys_win_then_the_wizard_fills_the_gaps(tmp_path):
    # Nothing anywhere: unconfigured, never an error.
    cfg = _cfg(tmp_path, "langs:\n  en:\n    label: E\n")
    resolved = mpt_control.resolve_commands(cfg)
    assert (resolved.restart, resolved.logs) == (None, None)
    assert resolved.logs_kind == "none"
    assert resolved.deployment is None

    # Wizard only: the derived lines are the commands.
    dep = mpt_control.MptDeployment(type="docker", name="mpt")
    mpt_control.save_deployment(cfg, dep)
    resolved = mpt_control.resolve_commands(cfg)
    assert resolved.restart == "docker restart mpt"
    assert resolved.logs == "docker logs -f mpt"
    assert resolved.logs_kind == "command"
    assert resolved.deployment == dep

    # Manual config.yaml key wins per command: restart from config.yaml,
    # logs still derived from the wizard's answer.
    cfg = _cfg(tmp_path, "web:\n  mpt_restart_command: supervisorctl restart mpt\n")
    resolved = mpt_control.resolve_commands(cfg)
    assert resolved.restart == "supervisorctl restart mpt"
    assert resolved.logs == "docker logs -f mpt"
    assert resolved.deployment == dep

    # tmux logs are a poller, not a command — and the manual restart key
    # still wins over the wizard's kill-session line.
    mpt_control.save_deployment(
        cfg, mpt_control.MptDeployment(type="tmux", name="mpt", start_command="python app.py")
    )
    resolved = mpt_control.resolve_commands(cfg)
    assert resolved.restart == "supervisorctl restart mpt"
    assert resolved.logs is None
    assert resolved.logs_kind == "tmux"

    # A broken web-settings.yaml is nothing, not an error: the manual key
    # survives, the derived lines are gone.
    (tmp_path / "web-settings.yaml").write_text(":::\n", encoding="utf-8")
    resolved = mpt_control.resolve_commands(cfg)
    assert resolved.restart == "supervisorctl restart mpt"
    assert resolved.logs_kind == "none"
    assert resolved.deployment is None


# ── TmuxPollStream: capture-pane polling with the same fan-out contract ──────


def _poller(snapshots, **kwargs):
    """A TmuxPollStream over scripted snapshots; None = a failed capture.

    When the script runs out, the capture stops the stream — a test's way
    of saying "the pane will never change again" without racing the poll
    thread on timing. The last pane repeats, so the final capture after the
    script runs dry adds no lines."""
    feed = iter(snapshots)
    holder: dict[str, mpt_control.TmuxPollStream] = {}
    last: dict[str, list[str] | None] = {"pane": None}

    def capture(session: str):
        try:
            last["pane"] = next(feed)
        except StopIteration:
            holder["stream"].stop()
        return last["pane"]

    stream = mpt_control.TmuxPollStream("mpt", interval=0.01, capture=capture, **kwargs)
    holder["stream"] = stream
    return stream


def test_tmux_poller_emits_only_new_lines(tmp_path):
    stream = _poller([["a", "b"], ["a", "b", "c"], ["a", "b", "c"], ["a", "b", "c", "d"]])
    stream.start()
    # Let the poll thread consume all four snapshots.
    collected = _collect(stream)
    assert collected == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]
    assert not stream.is_live()


def test_tmux_poller_survives_capture_failures_and_resyncs(tmp_path):
    # None snapshots are failures (session gone mid-restart); the poller
    # says so once, keeps polling, and picks the pane back up when it returns.
    stream = _poller(
        [
            ["a"],
            None,
            ["a", "b"],
            ["x"],  # pane rewritten: a resync marker, then the whole pane
        ]
    )
    stream.start()
    collected = _collect(stream)
    texts = [text for _, text in collected]
    assert "[web] tmux capture failed — is the session 'mpt' running?" in texts
    assert "[web] the pane is back" in texts
    assert "[web] the pane scrolled past the capture window; resynced" in texts
    assert "x" in texts
    assert not stream.is_live()


def test_tmux_poller_stop_reports_having_stopped_something():
    # An endless pane: the only way this stream ends is stop().
    feed = itertools.cycle([["tick"]])
    stream = mpt_control.TmuxPollStream("mpt", interval=60.0, capture=lambda session: next(feed))
    stream.start()
    assert stream.is_live()
    assert stream.stop() is True
    # Once the poll loop notices the stop, there is nothing left to stop —
    # a call racing the loop may re-signal, which is harmless (the same
    # property CommandStream.stop has on a dying child).
    deadline = time.monotonic() + 5.0
    while stream.is_live() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stream.stop() is False
