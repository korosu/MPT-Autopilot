"""
mpt_config.py — read MoneyPrinterTurbo's config.toml to reuse its settings
(credentials, platforms, etc.) so MPT-Autopilot users don't re-enter them.

Only the [app] section is needed for upload-post credentials.  A missing or
unreadable file is not fatal — the caller decides whether to proceed without it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# A version guard, not try/except: pyright follows sys.version_info and resolves
# the right module on both branches, so neither needs a `# type: ignore`.
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

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
    Pull the upload-post keys that are actually PRESENT in an ``[app]`` section.

    Returns a dict keyed exactly as MoneyPrinterTurbo's own ``[app]`` names
    them — the ``upload_post_`` prefix is part of each key, and batch/settings.py
    reads them under those names. Only keys the section really carries are
    returned: injecting a default for every key (the old behaviour) made the
    documented "omit the keys in config.yaml to fall back to MPT's config"
    flow a lie — the fallback always fired, so a missing MPT key and an
    explicitly-disabled one were indistinguishable (B-13). Callers decide
    their own defaults.
    """
    if not mpt_app:
        return {}
    return {key: mpt_app[key] for key in _REQUIRED_KEYS if key in mpt_app}
