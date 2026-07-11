"""engine/bgm.py — Background music discovery for MoneyPrinterTurbo."""

from __future__ import annotations

from pathlib import Path


def list_bgm_files(mpt_storage: Path, filter_str: str = "") -> list[tuple[str, int]]:
    """Return sorted list of (filename, size_bytes) for .mp3 files from resource/songs/."""
    songs_dir = mpt_storage / "resource" / "songs"
    if not songs_dir.exists():
        return []
    files = [(f.name, f.stat().st_size) for f in songs_dir.glob("*.mp3")]
    if filter_str:
        fl = filter_str.lower()
        files = [(n, s) for n, s in files if fl in n.lower()]
    return sorted(files, key=lambda x: x[0])
