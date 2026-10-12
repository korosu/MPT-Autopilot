"""
web/runner.py — one child process at a time, with its output fanned out.

The web layer never touches a process: it starts a run, subscribes to lines,
and cancels. Everything here is deliberately plain threading, because the
server is one process serving one person.

Three decisions worth knowing:

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
    ) -> None:
        self._spawn = spawner or (lambda argv: spawn(argv, cwd))
        self._slot = threading.Lock()
        self._cond = threading.Condition()
        self._lines: deque[tuple[int, str]] = deque(maxlen=buffer_lines)
        self._last_line = 0
        self._run_id = 0
        self._state: RunState | None = None
        self._proc: ChildProcess | None = None

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
        kill_tree(proc)
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

    # ── the reader thread ────────────────────────────────────────────────────

    def _append(self, text: str) -> None:
        with self._cond:
            self._last_line += 1
            self._lines.append((self._last_line, text))
            self._cond.notify_all()

    def _pump(self, proc: ChildProcess) -> None:
        code: int | None = None
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
