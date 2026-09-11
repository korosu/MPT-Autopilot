"""
mpt_config.py — read MoneyPrinterTurbo's config.toml to reuse its settings
(credentials, platforms, etc.) so MPT-Autopilot users don't re-enter them.

Only the [app] section is needed for upload-post credentials.  A missing or
unreadable file is not fatal — the caller decides whether to proceed without it.
"""

from __future__ import annotations

import logging
from pathlib import Path

try:
    import tomllib  # type: ignore[reportMissingImports]
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

_logger = logging.getLogger(__name__)

_REQUIRED_KEYS = (
    "upload_post_api_key",
    "upload_post_username",
    "upload_post_enabled",
    "upload_post_platforms",
    "upload_post_youtube_privacy_status",
)


def load_mpt_config(path: Path | None) -> dict[str, object] | None:
    """
    Read the ``[app]`` section from MPT's ``config.toml``.

    Returns ``None`` when the file is missing, unreadable, or has no ``[app]``
    section — the caller must handle that case.
    """
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        _logger.warning("MPT config.toml not found: %s", path)
        return None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _logger.warning("cannot read MPT config.toml (%s): %s", path, exc)
        return None
    app_section = raw.get("app")
    if not isinstance(app_section, dict):
        _logger.warning("MPT config.toml has no [app] section: %s", path)
        return None
    return dict(app_section)


def extract_upload_post(mpt_app: dict[str, object] | None) -> dict[str, object]:
    """
    Pull the upload-post keys out of an ``[app]`` section dict.

    Returns a dict with keys:
        enabled, api_key, username, platforms, youtube_privacy_status

    Missing keys fall back to safe defaults (disabled, empty strings, etc.).
    """
    defaults: dict[str, object] = {
        "upload_post_enabled": False,
        "upload_post_api_key": "",
        "upload_post_username": "",
        "upload_post_platforms": ["youtube", "tiktok"],
        "upload_post_youtube_privacy_status": "public",
    }
    if not mpt_app:
        return dict(defaults)
    return {key: mpt_app.get(key, defaults[key]) for key in _REQUIRED_KEYS}
