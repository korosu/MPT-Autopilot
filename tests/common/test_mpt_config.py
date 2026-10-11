"""Tests for mpt_config.py — reading MPT's own config.toml (part 11).

The module had no tests at all: batch/settings.py reads upload-post
credentials through it, so a regression here would surface as a confusing
"credentials missing" error two stages away.
"""

from __future__ import annotations

from pathlib import Path

from mpt_autopilot.mpt_config import extract_upload_post, load_mpt_config


def _toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_returns_none_without_a_path():
    assert load_mpt_config(None) is None


def test_load_returns_none_for_a_missing_file(tmp_path: Path):
    assert load_mpt_config(tmp_path / "absent.toml") is None


def test_load_reads_the_app_section(tmp_path: Path):
    path = _toml(
        tmp_path,
        '[app]\nupload_post_api_key = "sk-123"\nupload_post_username = "alice"\n[other]\nkey = 1\n',
    )
    app = load_mpt_config(path)
    assert app == {"upload_post_api_key": "sk-123", "upload_post_username": "alice"}


def test_load_returns_none_without_an_app_section(tmp_path: Path):
    path = _toml(tmp_path, "[other]\nkey = 1\n")
    assert load_mpt_config(path) is None


def test_load_returns_none_for_unparseable_toml(tmp_path: Path):
    path = _toml(tmp_path, "not [ valid toml ===")
    assert load_mpt_config(path) is None


def test_extract_returns_nothing_when_nothing_loaded():
    """B-13: no defaults injected — an absent key must stay absent so the
    caller can distinguish "MPT carries it" from "fall back to mine"."""
    assert extract_upload_post(None) == {}


def test_extract_returns_only_keys_the_section_actually_has():
    """B-13: the old code returned every key with a default, which made the
    documented "omit the keys in config.yaml to fall back to MPT's config"
    flow indistinguishable from a missing key."""
    values = extract_upload_post({"upload_post_api_key": "sk-1"})
    assert values == {"upload_post_api_key": "sk-1"}


def test_extract_ignores_irrelevant_keys():
    values = extract_upload_post(
        {
            "upload_post_enabled": True,
            "upload_post_api_key": "sk-9",
            "upload_post_username": "bob",
            "upload_post_platforms": ["youtube"],
            "upload_post_youtube_privacy_status": "unlisted",
            "irrelevant_key": "ignored",
        }
    )
    assert values["upload_post_enabled"] is True
    assert values["upload_post_api_key"] == "sk-9"
    assert values["upload_post_username"] == "bob"
    assert values["upload_post_platforms"] == ["youtube"]
    assert values["upload_post_youtube_privacy_status"] == "unlisted"
    assert "irrelevant_key" not in values


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
