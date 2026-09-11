"""
batch/upload_post.py — submit a rendered video to upload-post.com for
cross-posting to YouTube / TikTok / Instagram.

Reuses the same API MPT itself calls (see MoneyPrinterTurbo's
upload_post.py), but reads credentials from MPT-Autopilot's Settings
rather than from MPT's singleton config.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from mpt_autopilot.batch.settings import Settings

_UPLOAD_POST_BASE = "https://api.upload-post.com"
_UPLOAD_MAX_RETRIES = 3
_UPLOAD_BACKOFF_BASE = 5.0  # seconds; doubles each attempt


class UploadPostError(Exception):
    """Raised when upload-post.com returns a non-retryable error."""


class UploadPostUnavailable(Exception):
    """Raised when upload-post.com cannot be reached or credentials are missing."""


def upload_video(
    video_path: Path,
    meta_title: str,
    meta_description: str,
    meta_tags: list[str],
    settings: Settings,  # type: ignore[name-defined]
    *,
    privacy_status: str = "public",
    platforms: list[str] | None = None,
    log=lambda msg: None,
) -> dict:
    """
    Submit a rendered video to upload-post.com.

    Args:
        video_path: Path to the finished .mp4 file.
        meta_title: Video title (max 2200 chars, YouTube title max 100).
        meta_description: Video description.
        meta_tags: List of tag strings (with or without # prefix).
        settings: Batch Settings — must have upload_post_* fields populated.
        privacy_status: One of "public", "unlisted", "private".
        platforms: Target platforms. Defaults to settings.upload_post_platforms.
        log: Logger callable.

    Returns:
        {"success": bool, "request_id": str | None, "error": str | None}

    Raises:
        UploadPostUnavailable: credentials missing or endpoint not reachable.
        UploadPostError: 4xx (except 404) or non-transient 5xx.
    """
    # ── sanity checks ────────────────────────────────────────────────────────
    if not settings.upload_post_api_key or not settings.upload_post_username:
        raise UploadPostUnavailable(
            "upload-post credentials missing: set upload_post.api_key / username "
            "in config.yaml or configure upload_post in MPT's config.toml "
            "(see mpt.config_path)"
        )
    if not video_path.exists():
        raise UploadPostUnavailable(f"video file not found: {video_path}")

    if platforms is None:
        platforms = settings.upload_post_platforms or ["youtube", "tiktok"]

    title = meta_title[:2200]
    description = meta_description[:5000]

    # Build multipart form fields
    data: dict[str, Any] = {
        "user": settings.upload_post_username,
        "title": title,
        "privacy_level": "PUBLIC_TO_EVERYONE",
    }

    # platform[] fields — httpx multipart encodes a list[str] as repeated keys.
    platform_fields: list[str] = []
    for p in platforms:
        platform_fields.append(str(p))
    data["platform[]"] = platform_fields

    # YouTube-specific extras
    has_youtube = any(str(p).lower().startswith("youtube") for p in platforms)
    if has_youtube:
        youtube_title = title[:100]
        youtube_tags = [t.lstrip("#") for t in meta_tags if t and t.strip()]
        data.update(
            {
                "youtube_title": youtube_title,
                "youtube_description": description,
                "privacyStatus": privacy_status or settings.upload_post_youtube_privacy_status,
                "containsSyntheticMedia": "true",
            }
        )
        # tags[] as simple string list — httpx encodes as repeated multipart keys.
        tag_fields: list[str] = []
        for tag in youtube_tags[:100]:  # reasonable upper bound
            tag_fields.append(tag)
        if tag_fields:
            data["tags[]"] = tag_fields

    # ── HTTP call with retries ────────────────────────────────────────────────
    url = f"{_UPLOAD_POST_BASE}/api/upload"
    headers = {"Authorization": f"Apikey {settings.upload_post_api_key}"}

    last_exc: UploadPostError | None = None
    for attempt in range(1, _UPLOAD_MAX_RETRIES + 1):
        try:
            with open(video_path, "rb") as fh:
                files = {"video": (video_path.name, fh, "video/mp4")}
                r = httpx.post(
                    url,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=300,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            if attempt < _UPLOAD_MAX_RETRIES:
                delay = _UPLOAD_BACKOFF_BASE * (2 ** (attempt - 1))
                log(
                    f"  upload-post unreachable ({exc.__class__.__name__}),"
                    f" retrying in {delay:.0f}s",
                )
                time.sleep(delay)
                continue
            raise UploadPostUnavailable(
                f"upload-post unreachable after {attempt} attempts: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise UploadPostError(f"upload-post request failed: {exc}") from exc

        if r.status_code == 200:
            try:
                result = r.json()
            except ValueError:
                result = {"success": False, "error": r.text[:200] or "empty response"}
            if result.get("success"):
                log(
                    f"  upload-post queued: {video_path.name} "
                    f"(request_id={result.get('request_id', '?')})"
                )
            else:
                log(
                    f"  upload-post failed: {result.get('message', result.get('error', 'unknown'))}"
                )
            return result

        body_preview = (r.text or "")[:200].replace("\n", " ")
        err = UploadPostError(f"upload-post rejected (HTTP {r.status_code}): {body_preview}")

        if r.status_code == 404:
            raise UploadPostUnavailable(f"upload-post endpoint returned 404 (not available): {url}")

        # Retry on transient 5xx
        if r.status_code >= 500 and any(
            m in body_preview.lower() for m in ("temporary", "unavailable", "try again")
        ):
            if attempt < _UPLOAD_MAX_RETRIES:
                delay = _UPLOAD_BACKOFF_BASE * (2 ** (attempt - 1))
                log(
                    f"  upload-post transient error (HTTP {r.status_code}),"
                    f" retrying in {delay:.0f}s",
                )
                time.sleep(delay)
                last_exc = err
                continue
        raise err

    assert last_exc is not None
    raise last_exc
