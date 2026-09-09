"""Tests for engine/state.py — in-progress job tracking."""

from __future__ import annotations

from pathlib import Path

from mpt_autopilot.batch import state

_WORK_DIR = Path(__file__).parent


def _work(name: str) -> Path:
    return _WORK_DIR / name


def test_add_creates_entry():
    """Record in-progress job."""
    path = _work("test_state_add.txt")
    path.touch()
    try:
        state.add(path, "output.mp4", "task_123", 1)
        entries = state.list_all(path)
        assert len(entries) == 1
        assert entries[0]["output_file"] == "output.mp4"
        assert entries[0]["task_id"] == "task_123"
        assert entries[0]["attempt"] == 1
    finally:
        path.unlink(missing_ok=True)


def test_remove_filters_by_output_file():
    """Remove deletes entries for specific output_file."""
    path = _work("test_state_remove.txt")
    path.write_text(
        '{"output_file": "a.mp4", "task_id": "t1", "attempt": 1}\n'
        '{"output_file": "b.mp4", "task_id": "t2", "attempt": 1}\n',
        encoding="utf-8",
    )
    try:
        state.remove(path, "a.mp4")
        entries = state.list_all(path)
        assert len(entries) == 1
        assert entries[0]["output_file"] == "b.mp4"
    finally:
        path.unlink(missing_ok=True)


def test_list_all_empty():
    """Empty or missing file returns empty list."""
    path = _work("test_state_empty.txt")
    if path.exists():
        path.unlink()
    assert state.list_all(path) == []
