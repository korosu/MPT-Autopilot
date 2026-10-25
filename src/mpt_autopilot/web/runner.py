"""
web/runner.py — one child process at a time, with its output fanned out.

The web layer never touches a process: it starts a run, subscribes to lines,
and cancels. Everything here is deliberately plain threading, because the
server is one process serving one person.

Four decisions worth knowing:

  * The run slot is a `threading.Lock`, not lock.py's `file_lock`. file_lock
    treats a lock older than 60 s as abandoned, and a `batch` run takes tens of
    minutes — it would hand the slot to a second run mid-render.
    ponytail: in-process only. It does not stop a `mpt batch` started by hand in
    a terminal; the stages' own seen/jobs locks still cover that case.
  * Readers wait on a Condition over a line buffer, not on the pipe. A browser
    that reloads or disconnects therefore cannot stall or kill the run, and
    several tabs can watch the same output.
  * The reader thread owns the slot's release, so the lock is held for exactly
    as long as the process lives — including when it dies of an exception.
  * Cancel is graceful where the OS allows it, and the locks a hard kill
    orphaned are swept afterwards. A `mpt batch` holds `<seen stem>.run.lock`
    for the whole run (batch/run.py); a child killed without unwinding leaves
    that marker behind, and the next batch then fails with "another process
    appears to be writing it" until stale_after (30 min) expires. POSIX gets a
    real graceful signal (SIGINT → KeyboardInterrupt → the child's `finally`
    releases its locks); Windows has no usable one (see interrupt_group), so
    cancel kills hard and _sweep_locks removes the markers whose recorded PID
    is the child's — see lock.py's holder recording.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from mpt_autopilot.lock import sweep_orphan_locks

# How long a subscriber waits before emitting a keepalive tick. Short enough
# that a disconnected browser is noticed, long enough to stay quiet.
KEEPALIVE_SECONDS = 15.0

# W-7: SIGTERM is ignorable (a stuck ffmpeg or youtubeuploader child), and a
# process that survives it would hold the run slot forever. Escalate to
# SIGKILL after this grace period.
KILL_ESCALATION_SECONDS = 10.0

DEFAULT_BUFFER_LINES = 5000


class ChildProcess(Protocol):
    """
    The five members of Popen this module actually uses.

    Narrow on purpose: subprocess.Popen[str] satisfies it structurally, and a
    test can hand in a stand-in without a cast or a type: ignore.
    """

    @property
    def pid(self) -> int: ...

    @property
    def stdout(self) -> Iterable[str] | None: ...

    def wait(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


@dataclass(frozen=True)
class RunState:
    run_id: int
    stage: str
    started_at: float
    finished: bool = False
    exit_code: int | None = None
    cancelled: bool = False
    # The child's OS pid, so callers can tell this run apart from an mpt
    # process someone started in a terminal (external detection).
    pid: int | None = None


class RunnerBusy(RuntimeError):
    """A run is already in flight. One at a time, on purpose."""


def spawn(argv: list[str], cwd: Path | None = None) -> subprocess.Popen[str]:
    """
    Start `mpt` with a pipe that yields text as it is produced.

    PYTHONUNBUFFERED is what makes the log live: without it a stage's print()
    output sits in an 8 KB block buffer and arrives in bursts, which is useless
    for watching a poll loop.

    errors="replace" mirrors cli._force_utf8_output(): the child already writes
    UTF-8, and a mangled glyph beats aborting a 20-minute render.

    start_new_session puts the child in its own process group so cancel() can
    signal the group without signalling this server. Windows ignores it there;
    cancel() uses taskkill /T instead.
    """
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )


def interrupt_group(proc: ChildProcess) -> None:
    """
    Ask the child to stop *gracefully*, so its `finally` blocks run and the
    stage's file locks are released instead of being left behind for
    stale_after seconds.

    POSIX: SIGINT the child's own process group (spawn() put it there with
    start_new_session). CPython turns SIGINT into KeyboardInterrupt in the
    main thread; batch/run.py catches it around the run lock, so the marker is
    unlinked on the way out.

    Windows: nothing to send. Both console paths are dead ends (verified
    2026-10-22 against a local child): a bare CTRL_BREAK_EVENT kills the child
    outright with exit 0xC000013A — no unwinding at all — and with a SIGBREAK
    handler installed it does not interrupt the process. So Windows cancels
    hard (kill_tree) and _sweep_locks removes what the child could not.
    """
    if sys.platform == "win32":
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()


def kill_tree(proc: ChildProcess) -> None:
    """
    Kill the child and everything it started.

    A bare terminate() would leave the grandchildren running — the uploader
    shells out to `youtubeuploader`, and a half-cancelled upload is worse than
    no cancel button at all.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
        return
    # W-7: escalate from a daemon thread, so cancel() stays instant — the
    # thread waits out the grace period, then checks whether the child lived.
    threading.Thread(
        target=_escalate_to_sigkill,
        args=(proc,),
        name=f"mpt-web-kill-{proc.pid}",
        daemon=True,
    ).start()


def _escalate_after_grace(proc: ChildProcess) -> None:
    """
    Hard-kill a child that outlived the graceful signal (cancel's escalation).

    The graceful signal has had KILL_ESCALATION_SECONDS to land; a survivor
    goes through kill_tree, which sends SIGTERM and then escalates to SIGKILL
    on its own — two levels, so a wedged ffmpeg cannot hold the slot.
    """
    time.sleep(KILL_ESCALATION_SECONDS)
    if proc.poll() is None:
        kill_tree(proc)


def _escalate_to_sigkill(proc: ChildProcess) -> None:
    """
    SIGKILL the group if the child outlived the SIGTERM grace period (W-7).

    POSIX only — Windows cancels with `taskkill /F`, which is already forceful.
    The early return is also what lets type checkers resolve the POSIX-only
    `os.getpgid` / `signal.SIGKILL` on every platform they analyse.
    """
    if sys.platform == "win32":
        return
    time.sleep(KILL_ESCALATION_SECONDS)
    if proc.poll() is not None:
        return  # it died of SIGTERM after all
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()


class Runner:
    """One run slot, a line buffer, and any number of subscribers."""

    def __init__(
        self,
        spawner: Callable[[list[str]], ChildProcess] | None = None,
        cwd: Path | None = None,
        buffer_lines: int = DEFAULT_BUFFER_LINES,
        lock_dirs: Iterable[Path] | None = None,
    ) -> None:
        self._spawn = spawner or (lambda argv: spawn(argv, cwd))
        self._slot = threading.Lock()
        self._cond = threading.Condition()
        self._lines: deque[tuple[int, str]] = deque(maxlen=buffer_lines)
        self._last_line = 0
        self._run_id = 0
        self._state: RunState | None = None
        self._proc: ChildProcess | None = None
        # Directories whose `*.lock` markers are swept after a hard kill
        # (app.create_app derives them from the config's paths). Empty disables
        # the sweep — a runner nobody pointed at data dirs has nothing to clean.
        self._lock_dirs: list[Path] = list(lock_dirs or [])

    # ── queries ──────────────────────────────────────────────────────────────

    def state(self) -> RunState | None:
        with self._cond:
            return self._state

    def busy(self) -> bool:
        state = self.state()
        return state is not None and not state.finished

    # ── control ──────────────────────────────────────────────────────────────

    def start(self, stage: str, argv: list[str]) -> RunState:
        if not self._slot.acquire(blocking=False):
            raise RunnerBusy("a run is already in progress")
        try:
            proc = self._spawn(argv)
        except BaseException:
            self._slot.release()
            raise

        with self._cond:
            self._run_id += 1
            self._lines.clear()
            self._last_line = 0
            self._proc = proc
            state = RunState(run_id=self._run_id, stage=stage, started_at=time.time(), pid=proc.pid)
            self._state = state
            self._cond.notify_all()

        thread = threading.Thread(
            target=self._pump,
            args=(proc,),
            name=f"mpt-web-run-{state.run_id}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException as exc:
            # W-3: a failed thread start ("can't start new thread") used to
            # leave the slot held forever with a state that never finished —
            # every later start returned 409 and the child ran unattended.
            # The child is killed, the run is closed out, and the caller gets
            # an OSError, which app.py renders as a clean 500.
            kill_tree(proc)
            with self._cond:
                if self._state is not None:
                    self._state = replace(self._state, finished=True, exit_code=None)
                self._proc = None
                self._cond.notify_all()
            self._slot.release()
            raise OSError(f"could not start the run's reader thread: {exc}") from exc
        return state

    def cancel(self) -> bool:
        """True if a live run was signalled, False if there was nothing to stop."""
        with self._cond:
            proc, state = self._proc, self._state
            if proc is None or state is None or state.finished:
                return False
            self._state = replace(state, cancelled=True)
        if sys.platform == "win32":
            # No graceful console signal exists (interrupt_group explains why).
            # The child cannot release its file locks on the way out, so the
            # sweep in _pump's finally is what keeps <seen>.run.lock from
            # blocking the next batch for half an hour.
            kill_tree(proc)
        else:
            # SIGINT first: the child unwinds through its finally blocks and
            # releases the locks itself. A survivor still gets kill_tree after
            # the grace period (from a daemon thread, so cancel returns now).
            interrupt_group(proc)
            threading.Thread(
                target=_escalate_after_grace,
                args=(proc,),
                name=f"mpt-web-cancel-{proc.pid}",
                daemon=True,
            ).start()
        return True

    # ── output ───────────────────────────────────────────────────────────────

    def subscribe(self, run_id: int, last_id: int = 0) -> Iterator[tuple[int, str] | None]:
        """
        Yield `(line_id, text)` for `run_id` until it ends.

        A bare None is a keepalive tick: nothing new for KEEPALIVE_SECONDS. The
        caller uses it to write an SSE comment and notice a gone browser.

        Returns immediately for a run_id that is not the current one — the
        buffer only ever holds the latest run.
        """
        while True:
            with self._cond:
                state = self._state
                if state is None or state.run_id != run_id:
                    return
                pending = self._since(last_id)
                if not pending and not state.finished:
                    self._cond.wait(timeout=KEEPALIVE_SECONDS)
                    state = self._state
                    if state is None or state.run_id != run_id:
                        return
                    pending = self._since(last_id)
                finished = state.finished

            for item in pending:
                last_id = item[0]
                yield item

            if not pending:
                if finished:
                    return
                yield None

    def _since(self, last_id: int) -> list[tuple[int, str]]:
        # ponytail: linear scan of a bounded deque. Fine for 5k lines on
        # localhost; index by offset if the buffer ever grows.
        return [item for item in self._lines if item[0] > last_id]

    def _append(self, text: str) -> None:
        with self._cond:
            self._last_line += 1
            self._lines.append((self._last_line, text))
            self._cond.notify_all()

    def _sweep_locks(self, dead_pid: int | None) -> None:
        """
        Remove lock markers the hard-killed child left behind.

        Only markers naming THIS pid are touched — a live run's marker holds a
        different PID and stays. The lines are appended to the log so the user
        sees the cleanup instead of wondering why the next run failed.
        """
        if dead_pid is None or not self._lock_dirs:
            return
        for marker in sweep_orphan_locks(self._lock_dirs, dead_pid):
            self._append(
                f"[web] removed {marker.name} — its holder (PID {dead_pid}) is no longer running"
            )

    def _pump(self, proc: ChildProcess) -> None:
        code: int | None = None
        dead_pid = proc.pid
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    self._append(line.rstrip("\r\n"))
            code = proc.wait()
        except (OSError, ValueError) as exc:
            # A pipe that dies mid-read is a failed run, not a server crash.
            self._append(f"[web] lost the output stream: {exc}")
            code = proc.poll()
        finally:
            # Sweep BEFORE the state flips to finished: the sweep appends log
            # lines, and a subscriber that sees finished first would return
            # before they arrive. poll() returning None means the pipe died
            # while the child still lives (the OSError path above) — its
            # markers belong to a live process, not orphans, so no sweep.
            if proc.poll() is not None:
                self._sweep_locks(dead_pid)
            with self._cond:
                if self._state is not None:
                    # W-4: cancel() marks the run cancelled before the kill
                    # lands. A child that had already exited 0 in that window
                    # succeeded — reporting it as cancelled was a lie.
                    self._state = replace(
                        self._state,
                        finished=True,
                        exit_code=code,
                        cancelled=self._state.cancelled and code != 0,
                    )
                self._proc = None
                self._cond.notify_all()
            self._slot.release()
