"""
engine/settings.py

Loads .env (Telegram credentials, optional) and config.yaml (everything else,
including voice presets) into a single Settings object used across the package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from mpt_batch.engine import voices

_ROOT = Path.cwd()


@dataclass
class Settings:
    # MoneyPrinterTurbo connection — from config.yaml
    api_url: str
    mpt_storage: Path

    # Output — from config.yaml
    output_dir: Path
    seen_file: Path

    # Voice presets — every bundled Edge TTS voice plus config.yaml's `voices:`
    # section on top (config.yaml wins on name collisions), already flattened
    # into {alias: {tts_server, voice_name, ...}}. See engine/voices.py.
    voice_pool: dict[str, dict]

    # Logging — from config.yaml
    log_file: Path
    log_max_bytes: int

    # Timeouts — from config.yaml
    max_wait_seconds: int
    stuck_threshold_seconds: int

    # Retry behaviour — from config.yaml
    max_retries: int
    retry_delay_seconds: int
    max_consecutive_failures: int

    # Cache cleanup — from config.yaml
    cache_cleanup_enabled: bool
    cache_cleanup_interval: int

    # Telegram — token/chat_id from .env (optional), prefix from config.yaml
    telegram_token: str
    telegram_chat_id: str
    telegram_prefix: str

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def _require_cfg(cfg: dict, key: str) -> object:
    if key not in cfg:
        raise KeyError(
            f"config.yaml is missing required key '{key}'. "
            f"Check your config.yaml against the defaults in config.example.yaml."
        )
    return cfg[key]


def load(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or (_ROOT / ".env"))

    cfg_path = config_path or (_ROOT / "config.yaml")
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found: {cfg_path}\n"
            f"Copy config.example.yaml to config.yaml and adjust as needed."
        )

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # Resolve relative paths against config.yaml's location, not cwd, so the
    # tool behaves the same regardless of where the command is run from.
    cfg_dir = cfg_path.resolve().parent

    def _resolve(value: str) -> Path:
        return (cfg_dir / value).expanduser().resolve()

    s = Settings(
        api_url=str(_require_cfg(cfg, "api_url")).rstrip("/"),
        mpt_storage=_resolve(str(_require_cfg(cfg, "mpt_storage"))),
        output_dir=_resolve(cfg.get("output_dir", "./exports")),
        seen_file=_resolve(cfg.get("seen_file", "./seen.txt")),
        voice_pool=voices.build_full_pool(cfg.get("voices", {})),
        log_file=_resolve(cfg.get("log_file", "./logs/batch.log")),
        log_max_bytes=int(cfg.get("log_max_mb", 10)) * 1024 * 1024,
        max_wait_seconds=int(cfg.get("max_wait_seconds", 2400)),
        stuck_threshold_seconds=int(cfg.get("stuck_threshold_seconds", 3600)),
        max_retries=int(cfg.get("max_retries", 3)),
        retry_delay_seconds=int(cfg.get("retry_delay_seconds", 180)),
        max_consecutive_failures=int(cfg.get("max_consecutive_failures", 3)),
        cache_cleanup_enabled=(lambda v: v is True or v == 1)(
            cfg.get("cache_cleanup_enabled", True)
        ),
        cache_cleanup_interval=int(cfg.get("cache_cleanup_interval", 6)),
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_prefix=cfg.get("telegram_prefix", "mpt-batch"),
    )
    validate(s)
    return s


def validate(s: Settings) -> None:
    """Warn about likely configuration mistakes (does not raise)."""
    if not s.mpt_storage.exists():
        print(
            f"[mpt-batch] WARNING: mpt_storage '{s.mpt_storage}' does not exist — "
            "API file paths and fallback lookups will fail"
        )
    if not s.api_url.startswith("http"):
        print(f"[mpt-batch] WARNING: api_url '{s.api_url}' doesn't start with http — may not work")
