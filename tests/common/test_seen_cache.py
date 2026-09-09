"""
Tests for _contains_cached(): the cache-only fast path used by add() and
add_many() to avoid unnecessary file I/O on hot caches.

These tests avoid tmp_path (which is blocked by the session sandbox) by
injecting fake _cache entries and using a workspace-local file path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mpt_autopilot import seen

SANDBOX_DIR = Path(".pytest-sandbox-work")


@pytest.fixture(autouse=True)
def _clear_seen_cache():
    seen._cache.clear()
    yield
    seen._cache.clear()


def _work_path(name: str) -> Path:
    """A writable path inside the workspace for this test session."""
    SANDBOX_DIR.mkdir(exist_ok=True)
    return SANDBOX_DIR / name


# ── _contains_cached basics ────────────────────────────────────────────────────


def test_contains_cached_hit():
    """A name present in the cache returns True."""
    path = _work_path("hit_seen.txt")
    seen._cache[seen._key(path)] = _make_cache_entry(["a.mp4", "b.mp4"])
    assert seen._contains_cached(path, "a.mp4") is True


def test_contains_cached_miss():
    """A name not present in the cache returns False."""
    path = _work_path("miss_seen.txt")
    seen._cache[seen._key(path)] = _make_cache_entry(["a.mp4"])
    assert seen._contains_cached(path, "b.mp4") is False


def test_contains_cached_cold_start():
    """Empty cache → False (caller will fall back to load())."""
    path = _work_path("cold_seen.txt")
    assert seen._contains_cached(path, "any.mp4") is False


def test_contains_cached_after_clear():
    """After clear_cache(), _contains_cached returns False."""
    path = _work_path("clear_seen.txt")
    seen._cache[seen._key(path)] = _make_cache_entry(["a.mp4"])
    seen.clear_cache(path)
    assert seen._contains_cached(path, "a.mp4") is False


# ── add() / add_many() fast-path verification ─────────────────────────────────


def test_add_short_circuits_when_cached():
    """Regression: add() for a known entry must not call _read_entries."""
    path = _work_path("add_short.txt")
    path.write_text("existing.mp4\n", encoding="utf-8")
    seen.load(path)  # prime the cache

    reads = []
    original = seen._read_entries
    seen._read_entries = lambda p: (reads.append(p), original(p))[1]
    try:
        seen.add(path, "existing.mp4")
    finally:
        seen._read_entries = original

    assert reads == [], "add() cache hit must not read the file again"


def test_add_falls_back_to_load_on_cold_cache():
    """When cache is empty, add() must still read the file (idempotency)."""
    path = _work_path("add_cold.txt")
    path.write_text("existing.mp4\n", encoding="utf-8")

    reads = []
    original = seen._read_entries
    seen._read_entries = lambda p: (reads.append(p), original(p))[1]
    try:
        seen.add(path, "existing.mp4")
    finally:
        seen._read_entries = original

    assert len(reads) == 1, "cold cache add() should read the file once"


def test_add_many_short_circuits_when_all_cached():
    """add_many() with all known entries must not read the file."""
    path = _work_path("many_short.txt")
    path.write_text("a.mp4\nb.mp4\n", encoding="utf-8")
    seen.load(path)  # prime cache

    reads = []
    original = seen._read_entries
    seen._read_entries = lambda p: (reads.append(p), original(p))[1]
    try:
        seen.add_many(path, ["a.mp4", "b.mp4"])
    finally:
        seen._read_entries = original

    assert reads == [], "add_many() cache hit must not read the file"


def test_add_many_some_cached_some_new():
    """Mix of cached and new entries: only the new ones should be appended."""
    path = _work_path("many_mixed.txt")
    path.write_text("known.mp4\n", encoding="utf-8")
    seen.load(path)  # prime cache with "known.mp4"

    seen.add_many(path, ["known.mp4", "new.mp4"])
    result = seen.load(path)
    assert result == {"known.mp4", "new.mp4"}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_cache_entry(names: list[str], mtime: float = 1000.0, size: int = 10):
    """Inject a fake cache entry for a path."""
    return (mtime, size, list(names))
