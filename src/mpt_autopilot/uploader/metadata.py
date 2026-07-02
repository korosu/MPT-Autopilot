from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from yt_uploader.engine.settings import Defaults

TITLE_MAX_LEN = 100


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


def title_from_filename(video_path: Path) -> str:
    name = video_path.stem.replace("_", " ").replace("-", " ")
    name = " ".join(name.split())
    return name[:TITLE_MAX_LEN]


def load_meta(video_path: Path, defaults: Defaults) -> VideoMeta:
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
    """
    sidecar = sidecar_path(video_path)
    if not sidecar.exists():
        return VideoMeta(
            title=title_from_filename(video_path),
            description="",
            tags=list(defaults.tags),
            privacy_status=defaults.privacy_status,
            category_id=defaults.category_id,
        )

    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid sidecar metadata file {sidecar}: {exc}") from exc

    title = str(raw.get("title") or title_from_filename(video_path))[:TITLE_MAX_LEN]
    tags = raw.get("tags")
    tags = list(tags) if tags else list(defaults.tags)

    return VideoMeta(
        title=title,
        description=str(raw.get("description", "")),
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
