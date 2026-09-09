"""Tests for engine/seen.py — seen registry with caching."""

from __future__ import annotations

from pathlib import Path

from mpt_autopilot import seen

_WORK_DIR = Path(__file__).parent


def _work(name: str) -> Path:
    return _WORK_DIR / name


def test_load_empty_file():
    """Empty seen file returns empty set, no error."""
    path = _work("test_seen_empty.txt")
    path.touch()
    try:
        seen_set = seen.load(path)
        assert seen_set == set()
    finally:
        path.unlink(missing_ok=True)


def test_load_existing_files():
    """Load returns set of filenames from existing file."""
    path = _work("test_seen_existing.txt")
    path.write_text("video1.mp4\nvideo2.mp4\n", encoding="utf-8")
    try:
        seen_set = seen.load(path)
        assert seen_set == {"video1.mp4", "video2.mp4"}
    finally:
        path.unlink(missing_ok=True)


def test_add_creates_entry():
    """Add writes filename to file."""
    path = _work("test_seen_add.txt")
    try:
        seen.add(path, "new_video.mp4")
        assert seen.contains(path, "new_video.mp4")
    finally:
        path.unlink(missing_ok=True)


def test_add_idempotent():
    """Add same file twice doesn't duplicate."""
    path = _work("test_seen_idempotent.txt")
    try:
        seen.add(path, "video.mp4")
        seen.add(path, "video.mp4")
        content = path.read_text()
        assert content.count("video.mp4") == 1
    finally:
        path.unlink(missing_ok=True)


def test_add_updates_cached_set():
    """Add updates the cached set so dedup works mid-run."""
    path = _work("test_seen_cache.txt")
    seen._cache.clear()
    try:
        seen.add(path, "video1.mp4")
        # Second call should be idempotent (already in cache)
        seen.add(path, "video1.mp4")
        # Verify still only one entry
        assert seen.contains(path, "video1.mp4") is True
        assert len(seen.load(path)) == 1
    finally:
        path.unlink(missing_ok=True)
        seen._cache.clear()


def test_list_all_sorted():
    """list_all returns sorted list."""
    path = _work("test_seen_list.txt")
    path.write_text("zebra.mp4\nalpha.mp4\n", encoding="utf-8")
    try:
        assert seen.list_all(path) == ["alpha.mp4", "zebra.mp4"]
    finally:
        path.unlink(missing_ok=True)


def test_contains():
    """contains returns correct bool."""
    path = _work("test_seen_contains.txt")
    path.write_text("exists.mp4\n", encoding="utf-8")
    try:
        assert seen.contains(path, "exists.mp4") is True
        assert seen.contains(path, "missing.mp4") is False
    finally:
        path.unlink(missing_ok=True)
