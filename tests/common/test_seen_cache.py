from __future__ import annotations

from pathlib import Path

import pytest

from mpt_autopilot import seen

_WORK_DIR = Path(__file__).parent


def _work_path(name: str) -> Path:
    """Writable path next to this test file."""
    return _WORK_DIR / name


# ── _contains_cached basics ────────────────────────────────────────────────────


def test_contains_cached_hit():
    """A name present in the cache returns True."""
    path = _work_path("test_hit_seen.txt")
    seen._cache[seen._key(path)] = _make_cache_entry(["a.mp4", "b.mp4"])
    try:
        assert seen._contains_cached(path, "a.mp4") is True
    finally:
        path.unlink(missing_ok=True)


def test_contains_cached_miss():
    """A name not present in the cache returns False."""
    path = _work_path("test_miss_seen.txt")
    seen._cache[seen._key(path)] = _make_cache_entry(["a.mp4"])
    try:
        assert seen._contains_cached(path, "b.mp4") is False
    finally:
        path.unlink(missing_ok=True)


def test_contains_cached_cold_start():
    """Empty cache → False (caller will fall back to load())."""
    path = _work_path("test_cold_seen.txt")
    assert seen._contains_cached(path, "any.mp4") is False
    path.unlink(missing_ok=True)


def test_contains_cached_after_clear():
    """After clear_cache(), _contains_cached returns False."""
    path = _work_path("test_clear_seen.txt")
    seen._cache[seen._key(path)] = _make_cache_entry(["a.mp4"])
    seen.clear_cache(path)
    try:
        assert seen._contains_cached(path, "a.mp4") is False
    finally:
        path.unlink(missing_ok=True)


# ── add() / add_many() fast-path verification ─────────────────────────────────


def test_add_short_circuits_when_cached():
    """Regression: add() for a known entry must not call _read_entries."""
    path = _work_path("test_add_short.txt")
    path.write_text("existing.mp4\n", encoding="utf-8")
    seen.load(path)  # prime the cache

    reads = []
    original = seen._read_entries
    seen._read_entries = lambda p: (reads.append(p), original(p))[1]
    try:
        seen.add(path, "existing.mp4")
    finally:
        seen._read_entries = original
        path.unlink(missing_ok=True)

    assert reads == [], "add() cache hit must not read the file again"


def test_add_falls_back_to_load_on_cold_cache():
    """When cache is empty, add() must still read the file (idempotency)."""
    path = _work_path("test_add_cold.txt")
    path.write_text("existing.mp4\n", encoding="utf-8")

    reads = []
    original = seen._read_entries
    seen._read_entries = lambda p: (reads.append(p), original(p))[1]
    try:
        seen.add(path, "existing.mp4")
    finally:
        seen._read_entries = original
        path.unlink(missing_ok=True)

    assert len(reads) == 1, "cold cache add() should read the file once"


def test_add_many_short_circuits_when_all_cached():
    """add_many() with all known entries must not read the file."""
    path = _work_path("test_many_short.txt")
    path.write_text("a.mp4\nb.mp4\n", encoding="utf-8")
    seen.load(path)  # prime cache

    reads = []
    original = seen._read_entries
    seen._read_entries = lambda p: (reads.append(p), original(p))[1]
    try:
        seen.add_many(path, ["a.mp4", "b.mp4"])
    finally:
        seen._read_entries = original
        path.unlink(missing_ok=True)

    assert reads == [], "add_many() cache hit must not read the file"


def test_add_many_some_cached_some_new():
    """Mix of cached and new entries: only the new ones should be appended."""
    path = _work_path("test_many_mixed.txt")
    path.write_text("known.mp4\n", encoding="utf-8")
    seen.load(path)  # prime cache with "known.mp4"

    seen.add_many(path, ["known.mp4", "new.mp4"])
    result = seen.load(path)
    assert result == {"known.mp4", "new.mp4"}
    path.unlink(missing_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_cache_entry(names: list[str], mtime: float = 1000.0, size: int = 10):
    """Inject a fake cache entry for a path."""
    return (mtime, size, list(names))


@pytest.fixture(autouse=True)
def _clear_seen_cache():
    seen._cache.clear()
    yield
    seen._cache.clear()
