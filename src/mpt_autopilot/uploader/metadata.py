from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from yt_uploader.engine.settings import Defaults

_logger = logging.getLogger(__name__)

TITLE_MAX_LEN = 100
DESCRIPTION_MAX_LEN = 5000


@dataclass
class VideoMeta:
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy_status: str = "private"
    category_id: str = "22"


def sidecar_path(video_path: Path) -> Path:
    """<name>.mp4 -> <name>.json, next to the video."""
    return video_path.with_suffix(".json")


def title_from_filename(video_path: Path, account_name: str = "") -> str:
    """Convert video filename to title, stripping account suffix if present.

    Only strips a known suffix pattern (_<account_name>) to avoid
    mangling legitimate titles. If the stem doesn't end with this suffix,
    the title is derived as-is.
    """
    name = video_path.stem
    if account_name:
        suffix = f"_{account_name}"
        if name.lower().endswith(suffix.lower()):
            name = name[: len(name) - len(suffix)]
    name = name.replace("_", " ").replace("-", " ")
    name = " ".join(name.split())
    return name[:TITLE_MAX_LEN]


def _apply_tags_placement(existing_tags: list[str], sidecar_tags: list[str]) -> list[str]:
    """
    Apply sidecar hashtags to the hidden tags field.

    - Strips leading '#' from each sidecar tag (idempotent).
    - Merges with existing tags (existing first), case-insensitive dedup.
    - Trims to 500-char total budget (YouTube limit).
    """
    stripped = [t[1:] if t.startswith("#") else t for t in sidecar_tags]
    result = list(existing_tags)
    seen = set(t.lower() for t in result)
    for t in stripped:
        if t.lower() not in seen:
            result.append(t)
            seen.add(t.lower())
    # Trim to 500-char budget
    trimmed = []
    total_len = 0
    for t in result:
        if total_len + len(t) <= 500:
            trimmed.append(t)
            total_len += len(t)
        else:
            break
    return trimmed


def _apply_description_placement(title: str, description: str, sidecar_tags: list[str]) -> str:
    """
    Apply sidecar hashtags to the description as visible hashtags.

    - Counts existing '#\\\\w+' in title + description.
    - If 15 or more already exist, returns description unchanged (log warning).
    - Appends sidecar tags (as-is with '#') until 15 total.
    """
    existing_count = len(re.findall(r"#\w+", title + " " + description))
    if existing_count >= 15:
        _logger.warning(
            f"description already has {existing_count} hashtags, skipping all (YouTube limit)"
        )
        return description
    to_add = 15 - existing_count
    if to_add <= 0:
        return description
    add_tags = [t for t in sidecar_tags if t and (t.startswith("#") and len(t) > 1)][:to_add]
    if not add_tags:
        return description
    if to_add < len(sidecar_tags):
        _logger.info(f"dropping {len(sidecar_tags) - to_add} hashtags (exceed 15 limit)")
    return description + (" " if description else "") + " ".join(add_tags)


def load_meta(video_path: Path, defaults: Defaults, account_name: str = "") -> VideoMeta:
    """
    Reads <video>.json if present. The sidecar uses the same field names as
    youtubeuploader's own -metaJSON format, so no translation layer is needed:

        {
          "title": "...",
          "description": "...",
          "tags": ["...", "..."],
          "privacyStatus": "private",
          "categoryId": "22"
        }

    Any field can be omitted; missing fields fall back to config.yaml defaults.
    If there's no sidecar at all, the title is derived from the filename.

    When sidecar provides a "tags" list, applies hashtag_placement logic:
    - "tags": writes to hidden metadata tags
    - "description": appends as visible hashtags to description
    - "both": applies both independently

    If account_name is provided and the video filename ends with _<account_name>,
    that suffix is stripped before deriving the title.
    """
    sidecar = sidecar_path(video_path)
    if not sidecar.exists():
        return VideoMeta(
            title=title_from_filename(video_path, account_name),
            description="",
            tags=list(defaults.tags),
            privacy_status=defaults.privacy_status,
            category_id=defaults.category_id,
        )

    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid sidecar metadata file {sidecar}: {exc}") from exc

    title = str(raw.get("title") or title_from_filename(video_path, account_name))[:TITLE_MAX_LEN]
    description = str(raw.get("description", ""))[:DESCRIPTION_MAX_LEN]

    sidecar_tags = raw.get("tags")
    if sidecar_tags and isinstance(sidecar_tags, list) and len(sidecar_tags) > 0:
        placement = getattr(defaults, "hashtag_placement", "both")
        if placement == "tags":
            tags = _apply_tags_placement(list(defaults.tags), sidecar_tags)
        elif placement == "description":
            tags = list(defaults.tags)
            description = _apply_description_placement(title, description, sidecar_tags)
        else:  # "both"
            tags = _apply_tags_placement(list(defaults.tags), sidecar_tags)
            description = _apply_description_placement(title, description, sidecar_tags)
    else:
        tags = list(defaults.tags)

    return VideoMeta(
        title=title,
        description=description,
        tags=tags,
        privacy_status=str(raw.get("privacyStatus", defaults.privacy_status)),
        category_id=str(raw.get("categoryId", defaults.category_id)),
    )


def to_meta_json(meta: VideoMeta) -> str:
    return json.dumps(
        {
            "title": meta.title,
            "description": meta.description,
            "tags": meta.tags,
            "privacyStatus": meta.privacy_status,
            "categoryId": meta.category_id,
        },
        ensure_ascii=False,
    )
