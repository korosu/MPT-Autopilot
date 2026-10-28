"""Tests for required MoneyPrinterTurbo path settings in the `batch:` section."""

from __future__ import annotations

from pathlib import Path

import pytest

from mpt_autopilot.batch.settings import load
from mpt_autopilot.config import ConfigError

_BASE = "batch:\n  api_url: http://127.0.0.1:8080\n  mpt_storage: ./storage\n"


def test_load_requires_and_resolves_mpt_songs_dir(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_BASE + "  mpt_songs_dir: ./resource/songs\n", encoding="utf-8")

    settings = load(config, tmp_path / ".env")

    assert settings.mpt_storage == (tmp_path / "storage").resolve()
    assert settings.mpt_songs_dir == (tmp_path / "resource" / "songs").resolve()


def test_load_requires_mpt_songs_dir(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_BASE, encoding="utf-8")

    # The shared loader raises ConfigError (not KeyError) so the CLI can print a
    # message that names the section and points at config.example.yaml.
    with pytest.raises(ConfigError, match="batch.mpt_songs_dir"):
        load(config, tmp_path / ".env")


def test_load_requires_api_url(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("batch:\n  mpt_storage: ./storage\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="batch.api_url"):
        load(config, tmp_path / ".env")


def test_load_ignores_other_sections(tmp_path: Path) -> None:
    """A key under `pilot:` must not satisfy a required `batch:` key."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "pilot:\n  api_url: http://wrong\n" + _BASE + "  mpt_songs_dir: ./songs\n",
        encoding="utf-8",
    )

    settings = load(config, tmp_path / ".env")

    assert settings.api_url == "http://127.0.0.1:8080"


def test_upload_post_platforms_wraps_a_bare_string(tmp_path: Path) -> None:
    """
    A scalar `platforms: youtube` must become ["youtube"].

    It used to be cast() to list[str] without a check, and upload_post iterates
    the value — so a string went out as platform[]=y&platform[]=o&platform[]=u…
    """
    config = tmp_path / "config.yaml"
    config.write_text(
        _BASE + "  mpt_songs_dir: ./songs\n  upload_post:\n    platforms: youtube\n",
        encoding="utf-8",
    )

    settings = load(config, tmp_path / ".env")

    assert settings.upload_post_platforms == ["youtube"]


def test_upload_post_platforms_rejects_a_mapping(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        _BASE + "  mpt_songs_dir: ./songs\n  upload_post:\n    platforms:\n      a: b\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platforms must be a list"):
        load(config, tmp_path / ".env")


def test_poll_interval_defaults_to_ten_seconds(tmp_path: Path) -> None:
    """The hardcoded 10s wait_for_task poll became a setting (audit L);
    the default must keep the old behaviour."""
    config = tmp_path / "config.yaml"
    config.write_text(_BASE + "  mpt_songs_dir: ./songs\n", encoding="utf-8")

    settings = load(config, tmp_path / ".env")

    assert settings.poll_interval_seconds == 10


def test_upload_post_privacy_level_is_configurable(tmp_path: Path) -> None:
    """M-22: upload-post's privacy_level for non-YouTube platforms is a
    config.yaml setting, passed through verbatim."""
    config = tmp_path / "config.yaml"
    config.write_text(
        _BASE + "  mpt_songs_dir: ./songs\n  upload_post:\n    privacy_level: PRIVATE_TO_SELF\n",
        encoding="utf-8",
    )

    settings = load(config, tmp_path / ".env")

    assert settings.upload_post_privacy_level == "PRIVATE_TO_SELF"


def test_made_for_kids_defaults_to_false(tmp_path: Path) -> None:
    """MPT 1.3.9: upload-post.com requires an explicit made-for-kids boolean.
    Our default must be false ("not made for children"), independent of any
    youtube_privacy_status — the two settings are orthogonal."""
    config = tmp_path / "config.yaml"
    config.write_text(_BASE + "  mpt_songs_dir: ./songs\n", encoding="utf-8")

    settings = load(config, tmp_path / ".env")

    assert settings.upload_post_made_for_kids is False


def test_made_for_kids_is_configurable(tmp_path: Path) -> None:
    """A children's channel flips the declaration — same override chain as the
    other upload_post keys (config.yaml wins)."""
    config = tmp_path / "config.yaml"
    config.write_text(
        _BASE + "  mpt_songs_dir: ./songs\n  upload_post:\n    made_for_kids: true\n",
        encoding="utf-8",
    )

    settings = load(config, tmp_path / ".env")

    assert settings.upload_post_made_for_kids is True


def test_made_for_kids_falls_back_to_mpt_config_toml(tmp_path: Path) -> None:
    """B-13's contract for the whole upload_post family: config.yaml silent →
    MPT's config.toml [app] value is used (extract_upload_post returns only
    keys the section really carries)."""
    mpt_toml = tmp_path / "mpt.toml"
    mpt_toml.write_text(
        "[app]\nupload_post_youtube_made_for_kids = true\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        _BASE + "  mpt_songs_dir: ./songs\n" + "mpt:\n  config_path: mpt.toml\n",
        encoding="utf-8",
    )

    settings = load(config, tmp_path / ".env")

    assert settings.upload_post_made_for_kids is True
