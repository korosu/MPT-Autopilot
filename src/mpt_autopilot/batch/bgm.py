"""engine/bgm.py — Background music discovery for MoneyPrinterTurbo."""

from __future__ import annotations

from pathlib import Path


def list_bgm_files(mpt_storage: Path) -> list[str]:
    """Return sorted list of .mp3 filenames from resource/songs/."""
    songs_dir = mpt_storage / "resource" / "songs"
    if not songs_dir.exists():
        return []
    return sorted(f.name for f in songs_dir.glob("*.mp3"))
