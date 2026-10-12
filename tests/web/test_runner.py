"""
Tests for the run slot.

The spawner is injected, so none of this starts a real process, writes a file,
or touches the system temp folder — it all runs under the agent's sandbox. What
is being pinned is the state machine: the slot is held for exactly as long as
the process lives, subscribers can join late, and cancel marks the run.
"""

from __future__ import annotations

import signal
import threading
import time

import pytest

from mpt_autopilot.web import runner as runner_mod
from mpt_autopilot.web.runner import Runner, RunnerBusy


class FakeProc:
    """Enough of Popen for the Runner: a text pipe, a pid, and an exit code."""

    def __init__(self, lines: list[str], code: int = 0, hold: threading.Event | None = None):
        self.pid = 4242
        self.returncode: int | None = None
        self._code = code
        # Unset event = the pipe stays open after the last line, like a stage
        # that is still working.
        self._hold = hold if hold is not None else threading.Event()
        if hold is None:
            self._hold.set()
        self.stdout = self._emit(lines)

    def _emit(self, lines: list[str]):
        for line in lines:
            yield f"{line}\n"
        self._hold.wait(timeout=5)

    def wait(self) -> int:
        self.returncode = self._code
        return self._code

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:  # pragma: no cover - POSIX fallback only
        self._hold.set()


def _runner(*procs: FakeProc) -> Runner:
    """A Runner that hands out the given stand-in processes, in order."""
    queued = list(procs)
    return Runner(spawner=lambda argv: queued.pop(0))


def _finished(runner: Runner) -> bool:
    state = runner.state()
    return state is not None and state.finished


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def _drain(runner: Runner, run_id: int, last_id: int = 0) -> list[str]:
    return [item[1] for item in runner.subscribe(run_id, last_id) if item is not None]


# ── the happy path ────────────────────────────────────────────────────────────


def test_lines_reach_a_subscriber_in_order():
    runner = _runner(FakeProc(["one", "two", "three"]))
    state = runner.start("validate", ["mpt", "validate"])
    assert _drain(runner, state.run_id) == ["one", "two", "three"]


def test_final_state_carries_the_exit_code():
    runner = _runner(FakeProc(["boom"], code=2))
    state = runner.start("upload", ["mpt", "upload"])
    _drain(runner, state.run_id)
    final = runner.state()
    assert final is not None
    assert (final.finished, final.exit_code, final.cancelled) == (True, 2, False)


def test_a_late_subscriber_resumes_from_its_last_line():
    """What a browser does after F5: reconnect with Last-Event-ID."""
    runner = _runner(FakeProc(["a", "b", "c", "d"]))
    state = runner.start("batch", ["mpt", "batch"])
    _wait(lambda: _finished(runner))
    assert _drain(runner, state.run_id, last_id=2) == ["c", "d"]


def test_trailing_newlines_are_stripped_but_content_is_not():
    runner = _runner(FakeProc(["  indented", "[ERROR] нет config.yaml"]))
    state = runner.start("validate", ["mpt", "validate"])
    assert _drain(runner, state.run_id) == ["  indented", "[ERROR] нет config.yaml"]


def test_an_unknown_run_id_ends_immediately():
    runner = _runner(FakeProc(["x"]))
    state = runner.start("validate", ["mpt", "validate"])
    assert _drain(runner, state.run_id + 1) == []


# ── the slot ──────────────────────────────────────────────────────────────────


def test_a_second_start_is_refused_while_a_run_is_live():
    hold = threading.Event()
    runner = _runner(FakeProc(["working"], hold=hold))
    runner.start("batch", ["mpt", "batch"])
    _wait(runner.busy)
    with pytest.raises(RunnerBusy):
        runner.start("batch", ["mpt", "batch"])
    hold.set()


def test_the_slot_is_released_when_the_run_ends():
    runner = _runner(FakeProc(["done"]), FakeProc(["again"]))
    first = runner.start("validate", ["mpt", "validate"])
    _drain(runner, first.run_id)
    second = runner.start("validate", ["mpt", "validate"])
    assert second.run_id == first.run_id + 1


def test_a_failed_spawn_releases_the_slot():
    attempts = {"n": 0}
    proc = FakeProc(["recovered"])

    def spawner(argv: list[str]) -> FakeProc:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("no interpreter")
        return proc

    runner = Runner(spawner=spawner)
    with pytest.raises(OSError, match="no interpreter"):
        runner.start("validate", ["mpt", "validate"])
    assert not runner.busy()

    state = runner.start("validate", ["mpt", "validate"])
    assert _drain(runner, state.run_id) == ["recovered"]


def test_a_new_run_clears_the_previous_buffer():
    runner = _runner(FakeProc(["old"]), FakeProc(["new"]))
    first = runner.start("validate", ["mpt", "validate"])
    _drain(runner, first.run_id)
    second = runner.start("validate", ["mpt", "validate"])
    assert _drain(runner, second.run_id) == ["new"]


def test_the_state_carries_the_child_pid():
    """So the page can tell this run apart from one started in a terminal."""
    runner = _runner(FakeProc(["done"]))
    state = runner.start("validate", ["mpt", "validate"])
    assert state.pid == 4242


def test_a_failed_reader_thread_releases_the_slot(monkeypatch):
    """W-3: threading.Thread.start() raising used to wedge the slot forever —
    the child ran unattended and every later start answered 409."""
    proc = FakeProc(["never read"])
    killed: list[int] = []

    class NoStartThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(runner_mod.threading, "Thread", NoStartThread)
    monkeypatch.setattr(runner_mod, "kill_tree", lambda target: killed.append(target.pid))

    runner = Runner(spawner=lambda argv: proc)
    with pytest.raises(OSError, match="reader thread"):
        runner.start("batch", ["mpt", "batch"])
    assert killed == [4242]
    assert not runner.busy()
    final = runner.state()
    assert final is not None and final.finished


# ── cancel ────────────────────────────────────────────────────────────────────


def test_cancel_kills_the_tree_and_marks_the_run(monkeypatch):
    killed: list[int] = []
    hold = threading.Event()
    # code=1: a child that exits 0 in the cancel window counts as success
    # (W-4), so this run has to die with a failure for "cancelled" to hold.
    proc = FakeProc(["rendering"], code=1, hold=hold)

    def fake_kill(target):
        killed.append(target.pid)
        hold.set()  # a real kill closes the pipe; this stands in for that

    monkeypatch.setattr(runner_mod, "kill_tree", fake_kill)

    runner = _runner(proc)
    state = runner.start("batch", ["mpt", "batch"])
    _wait(runner.busy)
    assert runner.cancel() is True
    assert killed == [4242]

    _drain(runner, state.run_id)
    final = runner.state()
    assert final is not None
    assert (final.cancelled, final.finished) == (True, True)


def test_a_child_that_finished_successfully_is_not_reported_as_cancelled(monkeypatch):
    """W-4: cancel() marks cancelled before the kill lands; a child that had
    already exited 0 in that window succeeded — the page must say so."""
    hold = threading.Event()
    proc = FakeProc(["working"], hold=hold)  # exits 0 when the pipe closes
    monkeypatch.setattr(runner_mod, "kill_tree", lambda target: hold.set())

    runner = _runner(proc)
    state = runner.start("batch", ["mpt", "batch"])
    _wait(runner.busy)
    assert runner.cancel() is True

    _drain(runner, state.run_id)
    final = runner.state()
    assert final is not None
    assert (final.cancelled, final.exit_code) == (False, 0)


def test_cancel_with_nothing_running_is_a_no_op():
    runner = Runner()
    assert runner.cancel() is False


def test_cancel_after_the_run_finished_is_a_no_op(monkeypatch):
    monkeypatch.setattr(runner_mod, "kill_tree", lambda proc: pytest.fail("nothing to kill"))
    runner = _runner(FakeProc(["done"]))
    state = runner.start("validate", ["mpt", "validate"])
    _drain(runner, state.run_id)
    assert runner.cancel() is False


# ── SIGKILL escalation (W-7) ───────────────────────────────────────────────────


def test_a_child_that_survives_sigterm_is_escalated_to_sigkill(monkeypatch):
    proc = FakeProc(["stuck"])  # poll() stays None until wait()
    sent: list[tuple[int, object]] = []
    # Windows has no signal.SIGKILL and no os.getpgid/os.killpg, and the
    # escalation itself is POSIX-only; patch the platform and all three in so
    # this exercises the real branch on any host OS.
    sigkill = object()
    monkeypatch.setattr("mpt_autopilot.web.runner.sys.platform", "linux")
    monkeypatch.setattr("mpt_autopilot.web.runner.time.sleep", lambda seconds: None)
    monkeypatch.setattr("mpt_autopilot.web.runner.signal.SIGKILL", sigkill, raising=False)
    monkeypatch.setattr("mpt_autopilot.web.runner.os.getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        "mpt_autopilot.web.runner.os.killpg",
        lambda pgid, sig: sent.append((pgid, sig)),
        raising=False,
    )
    runner_mod._escalate_to_sigkill(proc)
    assert sent == [(4242, sigkill)]


def test_a_child_that_died_of_sigterm_is_left_alone(monkeypatch):
    proc = FakeProc(["done"])
    proc.wait()  # poll() now reports the exit code
    sent: list[tuple[int, object]] = []
    monkeypatch.setattr("mpt_autopilot.web.runner.sys.platform", "linux")
    monkeypatch.setattr("mpt_autopilot.web.runner.time.sleep", lambda seconds: None)
    monkeypatch.setattr("mpt_autopilot.web.runner.signal.SIGKILL", object(), raising=False)
    monkeypatch.setattr("mpt_autopilot.web.runner.os.getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        "mpt_autopilot.web.runner.os.killpg",
        lambda pgid, sig: sent.append((pgid, sig)),
        raising=False,
    )
    runner_mod._escalate_to_sigkill(proc)
    assert sent == []


def test_escalation_is_a_no_op_on_win32(monkeypatch):
    """taskkill /F is already forceful; there is nothing to escalate to."""
    sent: list[int] = []
    monkeypatch.setattr("mpt_autopilot.web.runner.sys.platform", "win32")
    monkeypatch.setattr("mpt_autopilot.web.runner.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "mpt_autopilot.web.runner.os.killpg",
        lambda pgid, sig: sent.append(sig),
        raising=False,
    )
    runner_mod._escalate_to_sigkill(FakeProc(["stuck"]))
    assert sent == []


def test_posix_cancel_escalates_when_the_child_survives(monkeypatch):
    proc = FakeProc(["stuck"])
    sent: list[tuple[int, object]] = []
    sigkill = object()
    monkeypatch.setattr("mpt_autopilot.web.runner.sys.platform", "linux")
    monkeypatch.setattr("mpt_autopilot.web.runner.time.sleep", lambda seconds: None)
    monkeypatch.setattr("mpt_autopilot.web.runner.signal.SIGKILL", sigkill, raising=False)
    monkeypatch.setattr("mpt_autopilot.web.runner.os.getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        "mpt_autopilot.web.runner.os.killpg",
        lambda pgid, sig: sent.append((pgid, sig)),
        raising=False,
    )

    runner_mod.kill_tree(proc)
    # the escalation thread is a daemon; with sleep patched out it lands
    # immediately, but still asynchronously.
    for _ in range(100):
        if len(sent) == 2:
            break
        time.sleep(0.01)
    assert sent == [(4242, signal.SIGTERM), (4242, sigkill)]


# ── keepalive ─────────────────────────────────────────────────────────────────


def test_a_quiet_run_yields_a_keepalive_tick(monkeypatch):
    monkeypatch.setattr(runner_mod, "KEEPALIVE_SECONDS", 0.05)
    hold = threading.Event()
    runner = _runner(FakeProc(["first"], hold=hold))
    state = runner.start("batch", ["mpt", "batch"])

    stream = runner.subscribe(state.run_id)
    assert next(stream) == (1, "first")
    assert next(stream) is None  # silence, not the end of the run
    hold.set()
    assert list(stream) == []
