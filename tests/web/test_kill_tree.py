"""Tests for web/runner.py's kill_tree — both platform branches (part 11).

The win32 taskkill branch had zero coverage (it cannot run on the POSIX
branch's tests and vice versa), yet it is the one a Windows user hits when
cancelling an upload mid-flight.
"""

from __future__ import annotations

import signal
from types import SimpleNamespace

from mpt_autopilot.web.runner import kill_tree


def test_kill_tree_on_win32_uses_taskkill(monkeypatch):
    monkeypatch.setattr("mpt_autopilot.web.runner.sys.platform", "win32")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "mpt_autopilot.web.runner.subprocess.run",
        lambda cmd, **kwargs: calls.append(cmd),
    )
    proc = SimpleNamespace(
        pid=4242,
        terminate=lambda: (_ for _ in ()).throw(AssertionError("bare terminate on win32")),
    )

    kill_tree(proc)  # type: ignore[arg-type]

    assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]


def test_kill_tree_on_posix_signals_the_process_group(monkeypatch):
    monkeypatch.setattr("mpt_autopilot.web.runner.sys.platform", "linux")
    # raising=False: os.getpgid/os.killpg do not exist on Windows at all, so
    # this test also runs there — it verifies the branch's logic, not the OS.
    monkeypatch.setattr("mpt_autopilot.web.runner.os.getpgid", lambda pid: 777, raising=False)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "mpt_autopilot.web.runner.os.killpg",
        lambda pgid, sig: kills.append((pgid, sig)),
        raising=False,
    )
    proc = SimpleNamespace(
        pid=99, terminate=lambda: (_ for _ in ()).throw(AssertionError("terminate before killpg"))
    )

    kill_tree(proc)  # type: ignore[arg-type]

    assert kills == [(777, signal.SIGTERM)]


def test_kill_tree_falls_back_to_terminate_when_group_missing(monkeypatch):
    monkeypatch.setattr("mpt_autopilot.web.runner.sys.platform", "linux")

    def boom(pid: int) -> int:
        raise ProcessLookupError(pid)

    monkeypatch.setattr("mpt_autopilot.web.runner.os.getpgid", boom, raising=False)
    # killpg must exist as an attribute too: the branch's expression looks it
    # up before evaluating the getpgid argument, and on Windows it is absent.
    monkeypatch.setattr(
        "mpt_autopilot.web.runner.os.killpg",
        lambda pgid, sig: (_ for _ in ()).throw(
            AssertionError("killpg reached despite the missing group")
        ),
        raising=False,
    )
    terminated: list[int] = []
    proc = SimpleNamespace(pid=99, terminate=lambda: terminated.append(99))

    kill_tree(proc)  # type: ignore[arg-type]

    assert terminated == [99]


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
