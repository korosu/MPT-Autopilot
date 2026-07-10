"""Tests for writer.py - hashtag persistence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hashtag_enricher.enricher.writer import build_hashtags_block, write_hashtags


def test_write_hashtags_creates_flat_tags(tmp_path: Path):
    """Basic: write hashtags, verify 'tags' key exists with correct content."""
    json_path = tmp_path / "test.json"
    hashtags_block = build_hashtags_block(
        tags_list=["#shorts", "#test", "#example"],
        language="English",
        model="gpt-4o-mini",
        source="filename",
    )
    write_hashtags(json_path, hashtags_block)

    data = json.loads(json_path.read_text())
    assert "hashtags" in data
    assert "tags" in data
    assert data["tags"] == ["#shorts", "#test", "#example"]


def test_write_hashtags_merges_with_existing_tags(tmp_path: Path):
    """Sidecar already has 'tags': ['#existing'], write new → result has both, deduped."""
    json_path = tmp_path / "test.json"
    existing = {"other": "data", "tags": ["#existing"]}
    json_path.write_text(json.dumps(existing))

    hashtags_block = build_hashtags_block(
        tags_list=["#shorts", "#existing"],  # #existing is duplicate
        language="English",
        model="gpt-4o-mini",
        source="filename",
    )
    write_hashtags(json_path, hashtags_block)

    data = json.loads(json_path.read_text())
    assert data["other"] == "data"
    assert "#existing" in data["tags"]
    assert "#shorts" in data["tags"]
    # Check dupe removed - should have exactly 2 tags
    assert len(data["tags"]) == 2
    # Existing first, then new
    assert data["tags"][0] == "#existing"


def test_write_hashtags_no_tags_key_initially(tmp_path: Path):
    """Sidecar has other keys but NO 'tags' → 'tags' is created fresh."""
    json_path = tmp_path / "test.json"
    json_path.write_text(json.dumps({"title": "My Video", "description": "..."}))

    hashtags_block = build_hashtags_block(
        tags_list=["#shorts", "#new"],
        language="English",
        model="gpt-4o-mini",
        source="filename",
    )
    write_hashtags(json_path, hashtags_block)

    data = json.loads(json_path.read_text())
    assert data["title"] == "My Video"
    assert data["description"] == "..."
    assert data["tags"] == ["#shorts", "#new"]


def test_write_hashtags_handles_malformed_tags(tmp_path: Path):
    """Existing 'tags' is a string, not list → overwrite correctly."""
    json_path = tmp_path / "test.json"
    json_path.write_text(json.dumps({"tags": "not-a-list"}))

    hashtags_block = build_hashtags_block(
        tags_list=["#shorts", "#fixed"],
        language="English",
        model="gpt-4o-mini",
        source="filename",
    )
    write_hashtags(json_path, hashtags_block)

    data = json.loads(json_path.read_text())
    # Should be overwritten with proper list
    assert data["tags"] == ["#shorts", "#fixed"]


def test_write_hashtags_creates_new_file(tmp_path: Path):
    """No sidecar exists → new file has both 'hashtags' and 'tags'."""
    hashtag_path = tmp_path / "video.mp4.json"

    hashtags_block = build_hashtags_block(
        tags_list=["#shorts"],
        language="English",
        model="gpt-4o-mini",
        source="filename",
    )
    write_hashtags(hashtag_path, hashtags_block)

    data = json.loads(hashtag_path.read_text())
    assert data["hashtags"]["tags_list"] == ["#shorts"]
    assert data["tags"] == ["#shorts"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
