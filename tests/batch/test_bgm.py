"""Tests for background music directory handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from mpt_autopilot.batch.bgm import list_bgm_files
from mpt_autopilot.batch.run import upload_bgm_cmd
from mpt_autopilot.batch.settings import Settings


def test_list_bgm_files_reads_configured_songs_directory(tmp_path: Path) -> None:
    songs_dir = tmp_path / "resource" / "songs"
    songs_dir.mkdir(parents=True)
    (songs_dir / "calm.mp3").write_bytes(b"calm")
    (songs_dir / "upbeat.mp3").write_bytes(b"upbeat")
    (songs_dir / "ignore.wav").write_bytes(b"wav")

    assert list_bgm_files(songs_dir) == [("calm.mp3", 4), ("upbeat.mp3", 6)]
    assert list_bgm_files(songs_dir, "calm") == [("calm.mp3", 4)]


def test_list_bgm_files_does_not_derive_path_from_storage(tmp_path: Path) -> None:
    storage_dir = tmp_path / "storage"
    songs_dir = tmp_path / "resource" / "songs"
    storage_dir.mkdir()
    songs_dir.mkdir(parents=True)
    (storage_dir / "resource").mkdir()
    (songs_dir / "real.mp3").write_bytes(b"music")

    assert list_bgm_files(songs_dir) == [("real.mp3", 5)]


def test_upload_bgm_uses_configured_songs_directory(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "custom.mp3").write_bytes(b"music")
    storage_dir = tmp_path / "storage"
    songs_dir = tmp_path / "resource" / "songs"

    settings = cast(Settings, SimpleNamespace(mpt_storage=storage_dir, mpt_songs_dir=songs_dir))
    upload_bgm_cmd(settings, source_dir)

    assert (songs_dir / "custom.mp3").read_bytes() == b"music"
    assert not (storage_dir / "resource" / "songs").exists()
