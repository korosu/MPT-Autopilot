"""
writer.py — persists the generated hashtags into {video_name}.json.

If the file already exists (e.g. script.json from MoneyPrinterTurbo),
the 'hashtags' key is merged in without touching any other fields.
If the file does not exist, it is created with only the 'hashtags' key.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)


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


def _backup_original(json_path: Path) -> None:
    """
    Move an unmergeable sidecar aside, to a path that does not exist yet.

    The old code unlinked an existing .bak first, so a second bad run
    overwrote the FIRST backup — the only surviving copy of the original.
    """
    backup = json_path.with_suffix(json_path.suffix + ".bak")
    n = 1
    while backup.exists():
        backup = json_path.with_suffix(f"{json_path.suffix}.bak.{n}")
        n += 1
    try:
        json_path.replace(backup)
    except OSError:
        return
    _logger.warning(
        "sidecar %s was not valid JSON (or not a JSON object); original "
        "saved to %s and replaced with hashtags only",
        json_path,
        backup,
    )


def write_hashtags(
    json_path: Path,
    hashtags_block: dict,
    extra_fields: dict | None = None,
) -> None:
    """
    Write (or merge) hashtags_block into json_path.

    Uses an atomic write (write to a sibling temp file, then os.replace) so
    that a crash or SIGINT between open and close never leaves a corrupt or
    zero-byte file.

    extra_fields are additional TOP-LEVEL keys written in the same atomic
    pass (A-4: title/description) — the uploader reads them from the sidecar's
    top level, next to the hashtags block.

    Args:
        json_path:       Target .json file path.
        hashtags_block:  The dict returned by build_hashtags_block().
        extra_fields:    Optional top-level keys to set (e.g. title, description).
    """
    # Load existing data if file exists
    existing: dict = {}
    if json_path.exists():
        loaded: object = None
        readable = True
        try:
            with open(json_path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, OSError):
            readable = False
        # A valid-JSON non-object (e.g. a top-level array) used to reach
        # `existing["hashtags"]` and crash with a TypeError this function's
        # except clauses never covered (M-36). It cannot be merged into —
        # same treatment as unparseable content.
        if readable and isinstance(loaded, dict):
            existing = loaded
        else:
            # Overwriting either kind of bad file silently would destroy
            # recoverable content (script.json may have held video_subject /
            # title / description that nothing else records). Back it up
            # first so the original is never lost.
            _backup_original(json_path)

    existing["hashtags"] = hashtags_block

    # Also write a flat "tags" key for downstream consumers (the upload stage).
    tags_list = hashtags_block["tags_list"]
    existing["tags"] = list(tags_list)

    # Optional top-level fields from the same pass (A-4): written after the
    # hashtags block so an enrich re-run refreshes them too.
    if extra_fields:
        for key, value in extra_fields.items():
            existing[key] = value

    # Atomic write: serialise to a sibling temp file then rename.
    # os.replace() is atomic on POSIX and Win32 (same filesystem).
    # mkstemp(dir=...) needs the parent to exist — create it defensively so a
    # first-ever call on a new sidecar (rare but possible in tests) doesn't
    # crash with FileNotFoundError mid-atomic-write.
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=json_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
            # fsync like seen.py/jobs.py: os.replace alone does not flush the
            # filesystem's own cache, so a power cut right after the rename
            # could still leave an empty file behind.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, json_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
