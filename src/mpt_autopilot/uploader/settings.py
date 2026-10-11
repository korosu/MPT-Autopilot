"""
uploader/settings.py — reads the `uploader:` section of the shared config.yaml
plus the separate accounts.yaml into a Settings dataclass.

accounts.yaml stays a separate file on purpose: it holds per-channel OAuth
secret paths and is the one file a user is most likely to keep outside the
repository.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mpt_autopilot import config as shared_config

SECTION = "uploader"

DEFAULT_ACCOUNTS_NAME = "accounts.yaml"
LEDGER_NAME = "yt-uploader-ledger.sqlite"

# YouTube's three privacy values. Lives here (not metadata.py) because both
# modules need it — settings validates the config.yaml default, metadata the
# sidecar value — and metadata already imports from settings, so this is the
# direction that keeps a single source of truth without an import cycle.
VALID_PRIVACY = frozenset({"public", "private", "unlisted"})


def _as_str_list(value: object, key: str) -> list[str]:
    """
    Normalise a config list: a bare string is wrapped (matching
    batch/settings._as_platform_list), anything but a list is a named error.
    `tags: shorts` used to be list()-ified into ['s','h','o','r','t','s']
    (M-44).
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        raise ValueError(
            f"config.yaml: uploader.defaults.{key} must be a list "
            f"(or a single value), got {type(value).__name__}"
        )
    return [str(item) for item in value if str(item).strip()]


@dataclass
class Account:
    name: str
    videos_dir: Path
    client_secrets: Path
    token_file: Path
    daily_upload_limit: int | None = None


@dataclass
class Defaults:
    privacy_status: str = "private"
    category_id: str = "22"
    tags: list[str] = field(default_factory=lambda: ["shorts"])
    hashtag_placement: str = "both"
    contains_synthetic_media: bool = True


@dataclass
class Settings:
    uploader_binary: Path
    meta_dir: Path
    sleep_between_uploads: int
    uploaded_dir_name: str
    defaults: Defaults
    telegram_token: str
    telegram_chat_id: str
    telegram_prefix: str
    ledger_path: Path
    accounts: dict[str, Account]
    keep_uploaded: bool = False


def _load_accounts_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy accounts.example.yaml to {path.name} and edit it."
        )
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(
    config_path: Path | None = None,
    accounts_path: Path | None = None,
    env_path: Path | None = None,
) -> Settings:
    cfg = shared_config.load(config_path, env_path)
    sec = cfg.section(SECTION)

    # accounts.yaml defaults to sitting next to config.yaml.
    if accounts_path is None:
        configured = sec.get("accounts_file")
        accounts_path = (
            cfg.resolve(str(configured)) if configured else cfg.dir / DEFAULT_ACCOUNTS_NAME
        )
    acc_raw = _load_accounts_yaml(accounts_path)

    defaults_raw = sec.get("defaults", {}) or {}
    hashtag_placement = defaults_raw.get("hashtag_placement", "both")
    if hashtag_placement not in {"tags", "description", "both"}:
        raise ValueError(
            f"config.yaml: uploader.defaults.hashtag_placement must be 'tags', "
            f"'description', or 'both', got '{hashtag_placement}'"
        )
    privacy_status = str(defaults_raw.get("privacy_status", "private")).strip().lower()
    if privacy_status not in VALID_PRIVACY:
        # The sidecar value is validated with a detailed message; the config
        # default used to pass straight through and get every video rejected
        # by YouTube one at a time (M-43).
        raise ValueError(
            f"config.yaml: uploader.defaults.privacy_status must be one of "
            f"{', '.join(sorted(VALID_PRIVACY))}, got {privacy_status!r}"
        )
    defaults = Defaults(
        privacy_status=privacy_status,
        category_id=str(defaults_raw.get("category_id", "22")),
        tags=_as_str_list(defaults_raw.get("tags", ["shorts"]), "tags"),
        hashtag_placement=hashtag_placement,
        contains_synthetic_media=bool(defaults_raw.get("contains_synthetic_media", True)),
    )

    sleep_between_uploads = int(sec.get("sleep_between_uploads", 5))
    if sleep_between_uploads < 0:
        raise ValueError(f"sleep_between_uploads must be >= 0, got {sleep_between_uploads}")

    accounts: dict[str, Account] = {}
    for name, raw in (acc_raw.get("accounts") or {}).items():
        if not isinstance(raw, dict):
            # `accounts: {main:}` parses as {main: None}; raw.get() on None is
            # an AttributeError the except clause below does not cover (M-45).
            raise ValueError(
                f"account '{name}' in {accounts_path.name} has no body — "
                f"expected videos_dir / client_secrets / token_file keys"
            )
        try:
            daily_limit = raw.get("daily_upload_limit")
            if daily_limit is not None and daily_limit <= 0:
                raise ValueError(
                    f"daily_upload_limit must be positive for account '{name}', got {daily_limit}"
                )
            accounts[name] = Account(
                name=name,
                # Resolved against config.yaml's directory (via cfg.resolve), not
                # cwd. Every other stage resolves its paths that way, and it is
                # what makes `mpt --config /srv/mpt/config.yaml upload` behave
                # identically from any working directory. Path(...).expanduser()
                # alone resolved against cwd, so a run from another directory
                # silently scanned and uploaded a different videos_dir.
                videos_dir=cfg.resolve(raw["videos_dir"]),
                client_secrets=cfg.resolve(raw["client_secrets"]),
                token_file=cfg.resolve(raw["token_file"]),
                daily_upload_limit=daily_limit,
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"account '{name}' in {accounts_path.name} has invalid config: {exc}"
            ) from exc

    meta_dir = cfg.require_path(SECTION, "meta_dir", "./meta")

    # Configurable (M-46); resolves relative to config.yaml's directory like
    # every other path. Default keeps the historical <config dir>/ name.
    ledger_path = cfg.path_value(SECTION, "ledger_path") or (cfg.dir / LEDGER_NAME)

    return Settings(
        uploader_binary=Path(sec.get("uploader_binary", "youtubeuploader")).expanduser(),
        meta_dir=meta_dir,
        sleep_between_uploads=sleep_between_uploads,
        uploaded_dir_name=sec.get("uploaded_dir_name", "old_videos"),
        defaults=defaults,
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        telegram_prefix=cfg.telegram_prefix(SECTION),
        ledger_path=ledger_path,
        accounts=accounts,
        keep_uploaded=bool(sec.get("keep_uploaded", False)),
    )


def get_account(settings: Settings, name: str) -> Account:
    if name not in settings.accounts:
        known = ", ".join(sorted(settings.accounts)) or "(none configured)"
        raise ValueError(f"unknown account '{name}'. Known accounts: {known}")
    return settings.accounts[name]


def _is_executable(path: Path) -> bool:
    """Return True if path exists and has executable permission (unix) or valid ext (win)."""
    if not path.exists():
        return False
    if os.name == "nt":
        return path.suffix.lower() in {".exe", ".cmd", ".bat"}
    return os.access(path, os.X_OK)


def find_uploader_binary(path: Path) -> Path | None:
    """
    Resolves the configured uploader binary: an explicit/relative path is
    checked directly, a bare command name (e.g. "youtubeuploader") is looked
    up on PATH. Returns None if it can't be found either way.
    """
    if path.is_absolute() or path.parent != Path("."):
        return path if _is_executable(path) else None
    resolved = shutil.which(str(path))
    return Path(resolved) if resolved else None


def validate_account_ready(settings: Settings, account: Account) -> str | None:
    """
    Returns a human-readable problem description if something required to
    actually attempt an upload is missing, or None if it's safe to proceed.
    Deliberately does NOT check token_file: that file is created by the first
    OAuth run and is legitimately absent before then - youtubeuploader itself
    will explain if it's missing when it matters.
    """
    if find_uploader_binary(settings.uploader_binary) is None:
        return (
            f"uploader_binary not found: {settings.uploader_binary} "
            "(check config.yaml, or install youtubeuploader and put it on PATH)"
        )
    if not account.client_secrets.exists():
        return f"client_secrets not found for account '{account.name}': {account.client_secrets}"
    return None
