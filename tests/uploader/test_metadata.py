"""Tests for metadata.py - sidecar parsing and hashtag placement."""

from __future__ import annotations

from pathlib import Path

import pytest

from yt_uploader.engine.metadata import (
    Defaults,
    _apply_description_placement,
    _apply_tags_placement,
    load_meta,
    title_from_filename,
)


def test_tags_placement_strips_hash_and_merges():
    """Sidecar tags have '#', tags field should strip it."""
    existing = ["shorts", "default"]
    sidecar = ["#shorts", "#new", "#NEW"]  # #shorts is dupe, #NEW dupes #new case-insens
    result = _apply_tags_placement(existing, sidecar)
    # Existing first, then new unique tags
    assert result == ["shorts", "default", "new"]


def test_tags_placement_500_char_budget():
    """Tags beyond 500 chars are dropped."""
    # 50 chars + 30 chars = 80, well under 500, keep all
    existing = [f"tag{i}" for i in range(5)]  # 10 chars total
    sidecar = ["a" * 15, "b" * 15]  # 30 chars
    result = _apply_tags_placement(existing, sidecar)
    assert len(result) == 7

    # Now test overflow
    existing = ["x" * 500]
    sidecar = ["#newtag"]
    # 500 already at limit, #newtag (6 chars) should be dropped
    result = _apply_tags_placement(existing, sidecar)
    assert result == ["x" * 500]


def test_description_placement_appends():
    """Hashtags are appended to description."""
    title = "My Video"
    description = "My description"
    sidecar = ["#shorts", "#fun"]
    result = _apply_description_placement(title, description, sidecar)
    assert result == "My description #shorts #fun"


def test_description_placement_15_limit():
    """Stops at 15 total hashtags in title+description."""
    # 10 existing, 5 to add = 15 exactly
    title = "#tag1 #tag2 #tag3 #tag4 #tag5"
    description = "#tag6 #tag7 #tag8 #tag9 #tag10"

    sidecar = ["#new1", "#new2", "#new3", "#new4", "#new5"]  # 5 tags to add
    result = _apply_description_placement(title, description, sidecar)
    # Should add all 5 to reach 15
    assert "#new5" in result


def test_description_placement_already_15():
    """No-op when 15 already present."""
    title = "#t1" * 15
    description = "desc"
    sidecar = ["#new"]
    result = _apply_description_placement(title, description, sidecar)
    assert result == "desc"  # unchanged, no hashtag appended


def test_load_meta_backward_compat_no_tags_key(tmp_path: Path):
    """Old sidecar (no 'tags' key) works as before."""
    sidecar = tmp_path / "video.json"
    sidecar.write_text('{"title": "My Video", "description": "test"}')
    video = tmp_path / "video.mp4"
    video.touch()

    defaults = Defaults()
    meta = load_meta(video, defaults)
    # defaults.tags = ["shorts"], no hashtag processing triggered
    assert meta.title == "My Video"
    assert meta.tags == ["shorts"]
    assert meta.description == "test"


def test_load_meta_backward_compat_empty_tags(tmp_path: Path):
    """Sidecar with empty 'tags' list → no hashtag processing."""
    sidecar = tmp_path / "video.json"
    sidecar.write_text('{"tags": []}')
    video = tmp_path / "video.mp4"
    video.touch()

    defaults = Defaults(tags=["default"])
    meta = load_meta(video, defaults)
    assert meta.tags == ["default"]


def test_both_placement_applies_both(tmp_path: Path):
    """Both placement modes work together."""
    sidecar = tmp_path / "video.json"
    sidecar.write_text('{"tags": ["#shorts", "#history"]}')
    video = tmp_path / "video.mp4"
    video.touch()

    defaults = Defaults(hashtag_placement="both")
    meta = load_meta(video, defaults)

    # Tags field: stripped, merged
    assert "shorts" in meta.tags
    assert "history" in meta.tags

    # Description: hashtags appended
    assert "#shorts" in meta.description
    assert "#history" in meta.description


def test_tags_only_placement(tmp_path: Path):
    """Only tags field is modified in 'tags' mode."""
    sidecar = tmp_path / "video.json"
    sidecar.write_text('{"tags": ["#fun"]}')
    video = tmp_path / "video.mp4"
    video.touch()

    defaults = Defaults(hashtag_placement="tags")
    meta = load_meta(video, defaults)

    assert "fun" in meta.tags
    assert meta.description == ""  # no change


def test_description_only_placement(tmp_path: Path):
    """Only description is modified in 'description' mode; tags uses defaults."""
    sidecar = tmp_path / "video.json"
    sidecar.write_text('{"tags": ["#fun"]}')
    video = tmp_path / "video.mp4"
    video.touch()

    defaults = Defaults(hashtag_placement="description")
    meta = load_meta(video, defaults)

    assert meta.tags == ["shorts"]  # defaults unchanged
    assert "#fun" in meta.description


def test_title_from_filename_strips_account_suffix():
    """Account suffix is stripped from the filename stem."""
    # With matching account suffix
    video = Path("mi_video_es.mp4")
    assert title_from_filename(video, "es") == "mi video"

    # Without account suffix (no strip)
    video = Path("my_cool_video.mp4")
    assert title_from_filename(video, "en") == "my cool video"

    # No account name provided (backward compat)
    video = Path("cool_video.mp4")
    assert title_from_filename(video) == "cool video"

    # Case-insensitive suffix match
    video = Path("test_video_ES.mp4")
    assert title_from_filename(video, "es") == "test video"


def test_load_meta_strips_suffix_from_filename(tmp_path: Path):
    """load_meta strips account suffix when deriving title from filename."""
    video = tmp_path / "my_subject_es.mp4"
    video.touch()

    defaults = Defaults()
    meta = load_meta(video, defaults, account_name="es")
    assert meta.title == "my subject"


def test_load_meta_no_strip_without_suffix(tmp_path: Path):
    """load_meta doesn't mangle title when filename lacks the account suffix."""
    video = tmp_path / "best_ai.mp4"
    video.touch()

    defaults = Defaults()
    meta = load_meta(video, defaults, account_name="en")
    assert meta.title == "best ai"


def test_load_meta_sidecar_without_title_uses_account_name(tmp_path: Path):
    """MPT script.json sidecars lack 'title', so suffix stripping still applies."""
    sidecar = tmp_path / "my_video_es.json"
    sidecar.write_text('{"tags": ["#test"]}')  # MPT-style sidecar (no title field)
    video = tmp_path / "my_video_es.mp4"
    video.touch()

    defaults = Defaults()
    meta = load_meta(video, defaults, account_name="es")
    assert meta.title == "my video"  # suffix stripped


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
