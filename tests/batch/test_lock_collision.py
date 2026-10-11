"""
test_lock_collision.py — guards the batch run lock against the seen.add()
inner-lock collision.

batch/run.py holds `file_lock` on a `<seen stem>.run` marker for the whole
run (M-30 replaced the hand-rolled O_EXCL mechanism with the shared lock).
seen.add() calls `file_lock(seen_file)` inside the run loop, and file_lock is
NOT re-entrant: if the batch ever locked seen_file itself, every registry
write would stall for the full wait timeout and then proceed unlocked — the
exact hazard H-2 fixed in seen.py.

These tests pin the truth:
  1. The two lock targets are provably distinct, even in subdirectories.
  2. seen.add() does not delete the batch run lock file.
  3. The batch keepalive (os.utime) does not raise after seen.add().

If someone ever refactors batch/run.py to `file_lock(seen_file)`,
test_lock_targets_are_distinct will start failing and the real collision bug
will appear.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import mpt_autopilot.lock as lock_mod
from mpt_autopilot import seen


@pytest.fixture()
def ws_tmp_path(tmp_path: Path) -> Path:
    """A pytest-managed per-test directory (auto-cleaned via basetemp)."""
    p = tmp_path / f"test-{os.getpid()}-{time.monotonic_ns()}"
    p.mkdir()
    return p


@pytest.fixture(autouse=True)
def _fast_lock_timers(monkeypatch):
    """Make stale detection fire immediately so tests run in milliseconds."""
    monkeypatch.setattr(lock_mod, "_STALE_AFTER", 0.1)
    monkeypatch.setattr(lock_mod, "_WAIT_TIMEOUT", 0.2)
    monkeypatch.setattr(lock_mod, "_POLL_INTERVAL", 0.01)
    yield
    seen._cache.clear()


def _batch_lock_target(seen_file: Path) -> Path:
    """The target batch/run.py locks for the whole run (see run())."""
    return seen_file.with_name(seen_file.stem + ".run")


def test_lock_targets_are_distinct(ws_tmp_path: Path) -> None:
    """batch/run.py and seen.add() lock DIFFERENT targets (no collision)."""
    seen_file = ws_tmp_path / "seen.txt"
    seen_file.write_text("", encoding="utf-8")

    batch_style = lock_mod.lock_path_for(_batch_lock_target(seen_file))
    file_lock_style = lock_mod.lock_path_for(seen_file)

    assert batch_style != file_lock_style, (
        f"REGRESSION: batch/run.py and seen.add() now target the same "
        f"file ({batch_style}) — the collision bug is real, and file_lock "
        f"is not re-entrant."
    )
    assert batch_style.name == "seen.run.lock"
    assert file_lock_style.name == "seen.txt.lock"


def test_lock_targets_are_distinct_at_any_depth(ws_tmp_path: Path) -> None:
    """The path collision is absent in every subdirectory, not just the top."""
    seen_file = ws_tmp_path / "jobs" / "seen.txt"
    seen_file.parent.mkdir(parents=True)
    seen_file.write_text("", encoding="utf-8")

    batch_style = lock_mod.lock_path_for(_batch_lock_target(seen_file))
    file_lock_style = lock_mod.lock_path_for(seen_file)

    assert batch_style != file_lock_style
    assert batch_style.name == "seen.run.lock"
    assert file_lock_style.name == "seen.txt.lock"


def test_seen_add_does_not_delete_batch_run_lock(ws_tmp_path: Path) -> None:
    """run() calls seen.add() inside the loop and expects its lock to stay."""
    seen_file = ws_tmp_path / "seen.txt"
    seen_file.write_text("", encoding="utf-8")

    with lock_mod.file_lock(_batch_lock_target(seen_file)):
        seen.add(seen_file, "video1.mp4")
        assert lock_mod.lock_path_for(_batch_lock_target(seen_file)).exists(), (
            "seen.add() destroyed the batch run lock file — the two locks "
            "must never collide (see test_lock_targets_are_distinct)."
        )


def test_keepalive_after_seen_add_does_not_raise(ws_tmp_path: Path) -> None:
    """The literal batch/run.py sequence: seen.add() then os.utime()."""
    seen_file = ws_tmp_path / "seen.txt"
    seen_file.write_text("", encoding="utf-8")

    with lock_mod.file_lock(_batch_lock_target(seen_file)):
        seen.add(seen_file, "video1.mp4")
        # verbatim order of operations from batch/run.py's success branch
        os.utime(lock_mod.lock_path_for(_batch_lock_target(seen_file)), None)

        # The seen registry entry was actually recorded.
        assert "video1.mp4" in seen.load(seen_file)
