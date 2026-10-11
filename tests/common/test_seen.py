"""
Tests for the unified seen registry: suffix resolution, insertion order,
idempotent appends, and the cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mpt_autopilot import seen


@pytest.fixture(autouse=True)
def _clear_seen_cache():
    seen._cache.clear()
    yield
    seen._cache.clear()


def test_resolve_without_suffix_is_seen_txt(tmp_path):
    assert seen.resolve(tmp_path) == tmp_path / "seen.txt"


def test_resolve_with_suffix_adds_slug():
    base = Path("/jobs")
    assert seen.resolve(base, "_es").name == "seen_es.txt"


def test_resolve_tolerates_missing_leading_underscore():
    base = Path("/jobs")
    assert seen.resolve(base, "es").name == "seen_es.txt"


def test_missing_file_is_empty_not_an_error(tmp_path):
    path = seen.resolve(tmp_path)
    assert seen.load(path) == set()
    assert seen.load_ordered(path) == []


def test_add_creates_the_file_and_is_idempotent(tmp_path):
    path = seen.resolve(tmp_path)
    seen.add(path, "a.mp4")
    seen.add(path, "a.mp4")

    assert path.read_text(encoding="utf-8").count("a.mp4") == 1
    assert seen.load(path) == {"a.mp4"}


def test_add_creates_missing_parent_directories(tmp_path):
    path = seen.resolve(tmp_path / "deep" / "nested")
    seen.add(path, "a.mp4")
    assert path.exists()


def test_load_ordered_preserves_insertion_order(tmp_path):
    path = seen.resolve(tmp_path)
    for name in ("first.mp4", "second.mp4", "third.mp4"):
        seen.add(path, name)

    assert seen.load_ordered(path) == ["first.mp4", "second.mp4", "third.mp4"]


def test_add_many_skips_known_entries(tmp_path):
    path = seen.resolve(tmp_path)
    seen.add(path, "a.mp4")
    seen.add_many(path, ["a.mp4", "b.mp4", "c.mp4"])

    assert seen.load_ordered(path) == ["a.mp4", "b.mp4", "c.mp4"]


def test_add_many_with_nothing_new_writes_nothing(tmp_path):
    path = seen.resolve(tmp_path)
    seen.add_many(path, ["a.mp4"])
    before = path.read_text(encoding="utf-8")
    seen.add_many(path, ["a.mp4"])

    assert path.read_text(encoding="utf-8") == before


def test_add_many_empty_list_does_not_create_the_file(tmp_path):
    path = seen.resolve(tmp_path)
    seen.add_many(path, [])
    assert not path.exists()


def test_reads_deduplicate_and_ignore_blank_lines(tmp_path):
    path = seen.resolve(tmp_path)
    path.write_text("a.mp4\n\na.mp4\n  b.mp4  \n", encoding="utf-8")

    assert seen.load_ordered(path) == ["a.mp4", "b.mp4"]


def test_contains(tmp_path):
    path = seen.resolve(tmp_path)
    seen.add(path, "a.mp4")
    assert seen.contains(path, "a.mp4")
    assert not seen.contains(path, "b.mp4")


def test_list_all_is_sorted(tmp_path):
    path = seen.resolve(tmp_path)
    seen.add_many(path, ["c.mp4", "a.mp4", "b.mp4"])
    assert seen.list_all(path) == ["a.mp4", "b.mp4", "c.mp4"]


def test_languages_do_not_share_a_registry(tmp_path):
    en = seen.resolve(tmp_path, "")
    es = seen.resolve(tmp_path, "_es")
    seen.add(en, "clip.mp4")

    assert seen.load(es) == set()


# ── Rotation ──────────────────────────────────────────────────────────────────


@pytest.fixture
def _small_rotation_limit():
    """Shrink the rotation threshold to its floor so a few hundred entries trip it."""
    previous = seen._rotation_max_bytes
    seen.set_rotation_max_bytes(1024)
    yield 1024
    seen._rotation_max_bytes = previous


def test_rotation_keeps_the_newest_entries_that_fit(tmp_path, _small_rotation_limit):
    path = seen.resolve(tmp_path)
    names = [f"{i:03d}.mp4" for i in range(200)]
    seen.add_many(path, names)

    seen.rotate(path)

    kept = seen.load_ordered(path)
    assert 1 < len(kept) < len(names)  # trimmed, but not down to a single entry
    assert kept == names[-len(kept) :]  # the newest survive, in order
    assert path.stat().st_size <= _small_rotation_limit
    # And as many as fit: one more entry would have pushed it over.
    one_more = seen._line_size(names[-len(kept) - 1])
    assert path.stat().st_size + one_more > _small_rotation_limit


def test_add_rotates_without_losing_the_whole_registry(tmp_path, _small_rotation_limit):
    """add() used to call rotate() under its own lock, which both stalled for
    the lock timeout and trimmed the file down to a single entry."""
    path = seen.resolve(tmp_path)
    seen.add_many(path, [f"{i:03d}.mp4" for i in range(200)])

    seen.add(path, "new.mp4")

    kept = seen.load_ordered(path)
    assert len(kept) > 1
    assert kept[-1] == "new.mp4"
    # The on-disk file, not just the cache, must match.
    seen.clear_cache(path)
    assert seen.load_ordered(path) == kept


def test_rotation_measures_bytes_not_characters(tmp_path, _small_rotation_limit):
    # "тест000.mp4" is 11 characters but 15 UTF-8 bytes; counting characters
    # would leave the file well over the byte budget the on-disk gate uses.
    path = seen.resolve(tmp_path)
    seen.add_many(path, [f"тест{i:03d}.mp4" for i in range(200)])

    seen.rotate(path)

    assert path.stat().st_size <= _small_rotation_limit


def test_rotation_is_a_no_op_under_the_limit(tmp_path):
    path = seen.resolve(tmp_path)
    seen.add_many(path, ["a.mp4", "b.mp4"])

    seen.rotate(path)

    assert seen.load_ordered(path) == ["a.mp4", "b.mp4"]
