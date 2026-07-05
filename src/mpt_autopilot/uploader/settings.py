from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


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


@dataclass
class Settings:
    uploader_binary: Path
    meta_dir: Path
    sleep_between_uploads: int
    uploaded_dir_name: str
    defaults: Defaults
    notify_enabled: bool
    telegram_token: str
    telegram_chat_id: str
    discord_webhook_url: str
    ledger_path: Path
    accounts: dict[str, Account]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy {path.name}.example to {path.name} and edit it."
        )
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(
    config_path: Path = Path("config.yaml"),
    accounts_path: Path = Path("accounts.yaml"),
    env_path: Path = Path(".env"),
) -> Settings:
    if env_path.exists():
        load_dotenv(env_path)

    cfg = _load_yaml(config_path)
    acc_raw = _load_yaml(accounts_path)

    defaults_raw = cfg.get("defaults", {}) or {}
    defaults = Defaults(
        privacy_status=defaults_raw.get("privacy_status", "private"),
        category_id=str(defaults_raw.get("category_id", "22")),
        tags=list(defaults_raw.get("tags", ["shorts"])),
    )

    notify_raw = cfg.get("notify", {}) or {}

    accounts: dict[str, Account] = {}
    for name, raw in (acc_raw.get("accounts") or {}).items():
        try:
            daily_limit = raw.get("daily_upload_limit")
            if daily_limit is not None and daily_limit <= 0:
                raise ValueError(
                    f"daily_upload_limit must be positive for account '{name}', got {daily_limit}"
                )
            accounts[name] = Account(
                name=name,
                videos_dir=Path(raw["videos_dir"]).expanduser(),
                client_secrets=Path(raw["client_secrets"]).expanduser(),
                token_file=Path(raw["token_file"]).expanduser(),
                daily_upload_limit=daily_limit,
            )
        except KeyError as exc:
            raise ValueError(
                f"account '{name}' in accounts.yaml is missing required field {exc}"
            ) from exc

    return Settings(
        uploader_binary=Path(cfg.get("uploader_binary", "youtubeuploader")).expanduser(),
        meta_dir=Path(cfg.get("meta_dir", "./meta")).expanduser(),
        sleep_between_uploads=int(cfg.get("sleep_between_uploads", 5)),
        uploaded_dir_name=cfg.get("uploaded_dir_name", "old_videos"),
        defaults=defaults,
        notify_enabled=bool(notify_raw.get("enabled", True)),
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        discord_webhook_url=notify_raw.get("discord_webhook_url", ""),
        ledger_path=config_path.parent / "yt-uploader-ledger.sqlite",
        accounts=accounts,
    )


def get_account(settings: Settings, name: str) -> Account:
    if name not in settings.accounts:
        known = ", ".join(sorted(settings.accounts)) or "(none configured)"
        raise ValueError(f"unknown account '{name}'. Known accounts: {known}")
    return settings.accounts[name]


def find_uploader_binary(path: Path) -> Path | None:
    """
    Resolves the configured uploader binary: an explicit/relative path is
    checked directly, a bare command name (e.g. "youtubeuploader") is looked
    up on PATH. Returns None if it can't be found either way.
    """
    if path.is_absolute() or path.parent != Path("."):
        return path if path.exists() else None
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
