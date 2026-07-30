"""Tests for required MoneyPrinterTurbo path settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from mpt_batch.engine.settings import load


def test_load_requires_and_resolves_mpt_songs_dir(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "api_url: http://127.0.0.1:8080\n"
        "mpt_storage: ./storage\n"
        "mpt_songs_dir: ./resource/songs\n",
        encoding="utf-8",
    )

    settings = load(config, tmp_path / ".env")

    assert settings.mpt_storage == (tmp_path / "storage").resolve()
    assert settings.mpt_songs_dir == (tmp_path / "resource" / "songs").resolve()


def test_load_requires_mpt_songs_dir(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "api_url: http://127.0.0.1:8080\n"
        "mpt_storage: ./storage\n",
        encoding="utf-8",
    )

    with pytest.raises(KeyError, match="mpt_songs_dir"):
        load(config, tmp_path / ".env")
