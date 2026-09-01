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
