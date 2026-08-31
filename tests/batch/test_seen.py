"""Tests for engine/seen.py — seen registry with caching."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mpt_batch.engine import seen


def test_load_empty_file():
    """Empty seen file returns empty set, no error."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    try:
        seen_set = seen.load(path)
        assert seen_set == set()
    finally:
        path.unlink()


def test_load_existing_files():
    """Load returns set of filenames from existing file."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("video1.mp4\nvideo2.mp4\n")
        path = Path(f.name)
    try:
        seen_set = seen.load(path)
        assert seen_set == {"video1.mp4", "video2.mp4"}
    finally:
        path.unlink()


def test_add_creates_entry():
    """Add writes filename to file."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    try:
        seen.add(path, "new_video.mp4")
        assert seen.contains(path, "new_video.mp4")
    finally:
        path.unlink()


def test_add_idempotent():
    """Add same file twice doesn't duplicate."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    try:
        seen.add(path, "video.mp4")
        seen.add(path, "video.mp4")
        content = path.read_text()
        assert content.count("video.mp4") == 1
    finally:
        path.unlink()


def test_add_updates_cached_set():
    """Add updates the cached set so dedup works mid-run."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    try:
        seen._cache.clear()
        seen.add(path, "video1.mp4")
        # Second call should be idempotent (already in cache)
        seen.add(path, "video1.mp4")
        # Verify still only one entry
        assert seen.contains(path, "video1.mp4") is True
        assert len(seen.load(path)) == 1
    finally:
        path.unlink()
        seen._cache.clear()


def test_list_all_sorted():
    """list_all returns sorted list."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("zebra.mp4\nalpha.mp4\n")
        path = Path(f.name)
    try:
        assert seen.list_all(path) == ["alpha.mp4", "zebra.mp4"]
    finally:
        path.unlink()


def test_contains():
    """contains returns correct bool."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("exists.mp4\n")
        path = Path(f.name)
    try:
        assert seen.contains(path, "exists.mp4") is True
        assert seen.contains(path, "missing.mp4") is False
    finally:
        path.unlink()
