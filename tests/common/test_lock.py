"""
Tests for lock.py — cross-platform advisory file lock.

The lock protects seen.txt and jobs.yaml from concurrent writes.  A bug here
corrupts state silently, so every code path is covered: acquire, release,
wait, timeout, stale detection, and strict mode.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

import mpt_autopilot.lock as lock_mod
from mpt_autopilot.lock import file_lock

# Use tiny values in tests so they run fast.  The real code uses 60s / 30s / 0.2s.
_STALE_AFTER = 2  # seconds — a lock file older than this is assumed abandoned
_WAIT_TIMEOUT = 1  # seconds — how long to wait for another process
_POLL_INTERVAL = 0.05  # seconds


@pytest.fixture(autouse=True)
def _patch_lock_timers(monkeypatch):
    """Monkey-patch the module constants so tests don't wait real minutes."""
    monkeypatch.setattr(lock_mod, "_STALE_AFTER", _STALE_AFTER)
    monkeypatch.setattr(lock_mod, "_WAIT_TIMEOUT", _WAIT_TIMEOUT)
    monkeypatch.setattr(lock_mod, "_POLL_INTERVAL", _POLL_INTERVAL)


# ── basic acquire / release ──────────────────────────────────────────────────


def test_lock_creates_and_removes_marker(tmp_path):
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    assert not lock_path.exists()
    with file_lock(target):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_nested_locks_sequential(tmp_path):
    """Two sequential with-blocks both succeed."""
    target = tmp_path / "data.txt"
    with file_lock(target):
        pass
    with file_lock(target):
        pass


# ── concurrent hold + wait ───────────────────────────────────────────────────


def test_second_lock_waits_for_first(tmp_path):
    """A second file_lock on the same target waits until the first releases."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    acquired_first = threading.Event()
    first_done = threading.Event()

    def hold_first():
        with file_lock(target, timeout=5):
            acquired_first.set()
            # Hold the lock for a bit so the second thread has time to wait.
            time.sleep(0.3)
        first_done.set()

    t = threading.Thread(target=hold_first)
    t.start()
    acquired_first.wait(timeout=2)
    assert lock_path.exists()

    # Second lock should block until the first releases.
    with file_lock(target, timeout=5):
        pass  # should be here after first_done

    t.join(timeout=2)
    assert first_done.is_set()
    assert not lock_path.exists()


# ── timeout behaviour ────────────────────────────────────────────────────────


def test_timeout_non_strict_proceeds_anyway(tmp_path):
    """When strict=False, a timeout prints a warning and proceeds without the lock."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    # Pre-create the lock to simulate another process holding it.
    lock_path.touch()

    # Use a very short timeout so the test doesn't hang.
    acquired = False
    with file_lock(target, timeout=0.1, strict=False):
        acquired = True

    assert acquired is True
    # The pre-existing lock file is NOT cleaned up (we didn't acquire it).
    assert lock_path.exists()
    lock_path.unlink()


def test_timeout_strict_raises(tmp_path):
    """When strict=True, a timeout raises TimeoutError."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    lock_path.touch()

    with pytest.raises(TimeoutError, match="Could not acquire lock"):
        with file_lock(target, timeout=0.1, strict=True):
            pass


def test_timeout_non_strict_cleans_up_own_lock(tmp_path):
    """After a non-strict timeout, if the lock was eventually acquired, it is removed."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    # Pre-create the lock, then remove it after a short delay so the first
    # file_lock picks it up.
    lock_path.touch()

    def remove_after_delay():
        time.sleep(0.15)
        lock_path.unlink(missing_ok=True)

    t = threading.Thread(target=remove_after_delay)
    t.start()

    # This should acquire the lock after the pre-existing one is removed.
    with file_lock(target, timeout=2, strict=False):
        assert lock_path.exists()

    t.join(timeout=2)
    # After the with-block, the lock should be removed.
    assert not lock_path.exists()


# ── stale detection ──────────────────────────────────────────────────────────


def test_stale_lock_is_removed(tmp_path):
    """A lock file older than _STALE_AFTER is treated as abandoned and removed."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    # Create a stale lock file.
    lock_path.touch()
    stale_time = time.time() - (_STALE_AFTER + 5)
    os.utime(lock_path, (stale_time, stale_time))

    # This should succeed immediately: the stale lock is detected and removed.
    with file_lock(target, timeout=0.1, strict=True):
        pass

    assert not lock_path.exists()


def test_non_stale_lock_is_not_removed_on_timeout(tmp_path):
    """A fresh lock file is not deleted when the timeout expires."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    # Create a fresh (non-stale) lock file.
    lock_path.touch()

    with pytest.raises(TimeoutError):
        with file_lock(target, timeout=0.1, strict=True):
            pass

    # The fresh lock is still there (we didn't acquire it).
    assert lock_path.exists()
    lock_path.unlink()


# ── lock content ─────────────────────────────────────────────────────────────


def test_lock_file_is_empty(tmp_path):
    """The lock file contains no data — it's just a marker."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    with file_lock(target):
        assert lock_path.stat().st_size == 0


def test_lock_is_per_target(tmp_path):
    """Locks on different targets don't interfere."""
    target_a = tmp_path / "a.txt"
    target_b = tmp_path / "b.txt"

    with file_lock(target_a):
        with file_lock(target_b):
            pass  # both locks held simultaneously


# ── FileNotFoundError race ───────────────────────────────────────────────────


def test_lock_released_between_check_and_stat(tmp_path):
    """If the lock file is removed between our O_EXCL failure and stat(), we retry."""
    target = tmp_path / "data.txt"
    lock_path = target.with_name(target.name + ".lock")

    # Create the lock, then remove it very quickly.  The file_lock should
    # detect the FileNotFoundError on stat() and retry the O_EXCL.
    lock_path.touch()

    def remove_after_delay():
        time.sleep(0.05)
        lock_path.unlink(missing_ok=True)

    t = threading.Thread(target=remove_after_delay)
    t.start()

    # This should succeed: the race is detected and handled.
    with file_lock(target, timeout=2):
        assert lock_path.exists()

    t.join(timeout=2)
    assert not lock_path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
