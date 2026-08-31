"""Tests for engine/state.py — in-progress job tracking."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mpt_batch.engine import state


def test_add_creates_entry():
    """Record in-progress job."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    try:
        state.add(path, "output.mp4", "task_123", 1)
        entries = state.list_all(path)
        assert len(entries) == 1
        assert entries[0]["output_file"] == "output.mp4"
        assert entries[0]["task_id"] == "task_123"
        assert entries[0]["attempt"] == 1
    finally:
        path.unlink()


def test_remove_filters_by_output_file():
    """Remove deletes entries for specific output_file."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write('{"output_file": "a.mp4", "task_id": "t1", "attempt": 1}\n')
        f.write('{"output_file": "b.mp4", "task_id": "t2", "attempt": 1}\n')
        path = Path(f.name)
    try:
        state.remove(path, "a.mp4")
        entries = state.list_all(path)
        assert len(entries) == 1
        assert entries[0]["output_file"] == "b.mp4"
    finally:
        path.unlink()


def test_list_all_empty():
    """Empty or missing file returns empty list."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    try:
        path.unlink()
        assert state.list_all(path) == []
    finally:
        if path.exists():
            path.unlink()
