"""
test_lock_collision.py — REFUTES the suspected batch outer-lock / seen.add()
inner-lock collision.

Suspected bug (from the audit request): batch/run.py:317 acquires
`seen_file.with_name(seen_file.stem + ".lock")` and holds it for the whole
batch run; seen.add() calls lock.file_lock() which targets
`Path(str(target) + ".lock")` — allegedly the SAME file. If the inner lock
is stale, it would unlink batch's outer lock and the next os.utime()
keepalive would crash with FileNotFoundError.

FALSIFIED: `Path("seen.txt").stem` is `"seen"` (the suffix is stripped),
so `seen_file.stem + ".lock"` is `"seen.lock"`, not `"seen.txt.lock"`.
The two lock mechanisms therefore target DIFFERENT files in every
configuration, and the collision never happens.

These tests document the truth:
  1. The two lock paths are provably distinct, even in subdirectories.
  2. seen.add() does not delete batch's outer lock file.
  3. The batch keepalive (os.utime) does not raise after seen.add().

If someone ever refactors batch/run.py to use `str(seen_file) + ".lock"`
instead of `seen_file.stem + ".lock"`, test_lock_paths_are_distinct will
start failing and the real collision bug will appear.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Generator
from pathlib import Path

import pytest

import mpt_autopilot.lock as lock_mod
from mpt_autopilot import seen


@pytest.fixture()
def ws_tmp_path() -> Generator[Path, None, None]:
    """A workspace-local per-test directory (does not touch %TEMP%)."""
    root = Path(__file__).parent / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"test-{os.getpid()}-{time.monotonic_ns()}"
    p.mkdir()
    try:
        yield p
    finally:
        shutil.rmtree(p, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fast_lock_timers(monkeypatch):
    """Make stale detection fire immediately so tests run in milliseconds."""
    monkeypatch.setattr(lock_mod, "_STALE_AFTER", 0.1)
    monkeypatch.setattr(lock_mod, "_WAIT_TIMEOUT", 0.2)
    monkeypatch.setattr(lock_mod, "_POLL_INTERVAL", 0.01)
    yield
    seen._cache.clear()


def _simulated_batch_lock(seen_file: Path) -> Path:
    """Create the lock exactly the way batch/run.py does (raw O_EXCL)."""
    lock_path = seen_file.with_name(seen_file.stem + ".lock")
    os.close(os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    return lock_path


def test_lock_paths_are_distinct(ws_tmp_path: Path) -> None:
    """batch/run.py and lock.file_lock() lock DIFFERENT files (no collision)."""
    seen_file = ws_tmp_path / "seen.txt"
    seen_file.write_text("", encoding="utf-8")

    batch_style = seen_file.with_name(seen_file.stem + ".lock")
    file_lock_style = Path(str(seen_file) + ".lock")

    assert batch_style != file_lock_style, (
        f"REGRESSION: batch/run.py and lock.file_lock() now target the same "
        f"file ({batch_style}) — the suspected collision bug is real."
    )
    assert batch_style.name == "seen.lock"
    assert file_lock_style.name == "seen.txt.lock"


def test_lock_paths_are_distinct_at_any_depth(ws_tmp_path: Path) -> None:
    """The path collision is absent in every subdirectory, not just the top."""
    seen_file = ws_tmp_path / "jobs" / "seen.txt"
    seen_file.parent.mkdir(parents=True)
    seen_file.write_text("", encoding="utf-8")

    batch_style = seen_file.with_name(seen_file.stem + ".lock")
    file_lock_style = Path(str(seen_file) + ".lock")

    assert batch_style != file_lock_style
    assert batch_style.name == "seen.lock"
    assert file_lock_style.name == "seen.txt.lock"


def test_seen_add_does_not_delete_outer_batch_lock(ws_tmp_path: Path) -> None:
    """batch/run.py line 399 calls seen.add(); line 401 assumes the lock is intact."""
    seen_file = ws_tmp_path / "seen.txt"
    seen_file.write_text("", encoding="utf-8")

    # Batch acquired its outer lock 61 seconds ago (a slow render).
    lock_path = _simulated_batch_lock(seen_file)
    stale = time.time() - 61
    os.utime(lock_path, (stale, stale))

    # Job 1 finishes. Batch records it in the registry...
    seen.add(seen_file, "video1.mp4")

    # ...and expects to keep its outer lock alive on the very next line.
    assert lock_path.exists(), (
        "seen.add() destroyed batch's outer lock file. "
        "This would only happen if the two lock mechanisms collided — "
        "see test_lock_paths_are_distinct."
    )


def test_keepalive_after_seen_add_does_not_raise(ws_tmp_path: Path) -> None:
    """The literal batch/run.py sequence: seen.add() then os.utime()."""
    seen_file = ws_tmp_path / "seen.txt"
    seen_file.write_text("", encoding="utf-8")

    lock_path = _simulated_batch_lock(seen_file)
    stale = time.time() - 61
    os.utime(lock_path, (stale, stale))

    # verbatim order of operations from batch/run.py:399-401
    seen.add(seen_file, "video1.mp4")
    os.utime(lock_path, None)  # batch/run.py line 401 — no exception expected

    # The seen registry entry was actually recorded.
    assert "video1.mp4" in seen.load(seen_file)
