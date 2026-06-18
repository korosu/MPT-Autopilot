"""
writer.py — persists the generated hashtags into {video_name}.json.

If the file already exists (e.g. script.json from MoneyPrinterTurbo),
the 'hashtags' key is merged in without touching any other fields.
If the file does not exist, it is created with only the 'hashtags' key.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def build_hashtags_block(
    tags_list: list[str],
    language: str,
    model: str,
    source: str,
) -> dict:
    """Build the dict that will be stored under the 'hashtags' key."""
    tags_string = " ".join(tags_list)
    return {
        "tags_list": tags_list,
        "tags_string": tags_string,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model,
        "detected_language": language,
        "source": source,
    }


def write_hashtags(
    json_path: Path,
    hashtags_block: dict,
    dry_run: bool = False,
) -> None:
    """
    Write (or merge) hashtags_block into json_path.

    Args:
        json_path:       Target .json file path.
        hashtags_block:  The dict returned by build_hashtags_block().
        dry_run:         If True, print what would be written but do nothing.
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

    if dry_run:
        print(f"  [dry-run] would write to {json_path}:")
        print(f"  {json.dumps({'hashtags': hashtags_block}, ensure_ascii=False, indent=4)}")
        return

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
