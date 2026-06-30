"""
writer.py — persists the generated hashtags into {video_name}.json.

If the file already exists (e.g. script.json from MoneyPrinterTurbo),
the 'hashtags' key is merged in without touching any other fields.
If the file does not exist, it is created with only the 'hashtags' key.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def build_hashtags_block(
    tags_list: list[str],
    language: str,
    model: str,
    source: str,
    platform: str = "youtube",
) -> dict:
    """Build the dict that will be stored under the 'hashtags' key."""
    tags_string = " ".join(tags_list)
    return {
        "tags_list": tags_list,
        "tags_string": tags_string,
        "tag_count": len(tags_list),
        "platform": platform,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model,
        "detected_language": language,
        "source": source,
    }


def write_hashtags(
    json_path: Path,
    hashtags_block: dict,
) -> None:
    """
    Write (or merge) hashtags_block into json_path.

    Uses an atomic write (write to a sibling temp file, then os.replace) so
    that a crash or SIGINT between open and close never leaves a corrupt or
    zero-byte file.

    Args:
        json_path:       Target .json file path.
        hashtags_block:  The dict returned by build_hashtags_block().
    """
    # Load existing data if file exists
    existing: dict = {}
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["hashtags"] = hashtags_block

    # Atomic write: serialise to a sibling temp file then rename.
    # os.replace() is atomic on POSIX and Win32 (same filesystem).
    tmp_fd, tmp_path = tempfile.mkstemp(dir=json_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
