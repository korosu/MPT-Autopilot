"""
batch/settings.py

Reads the `batch:` section of the shared config.yaml (plus the shared `langs:`
and `paths:` blocks) and the Telegram credentials from .env into a single
Settings object used across the batch stage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mpt_autopilot import config as shared_config
from mpt_autopilot.batch import voices

SECTION = "batch"


@dataclass
class Settings:
    # MoneyPrinterTurbo connection — from config.yaml
    api_url: str
    mpt_storage: Path
    mpt_songs_dir: Path

    # Output — from config.yaml
    output_dir: Path
    seen_file: Path

    # Per-language overrides — from the shared `langs:` block (optional)
    # {code: {"file_suffix": "_es", ...}} — empty dict when absent
    langs: dict[str, dict]

    # Default jobs directory — from config.yaml (optional)
    jobs_dir: Path | None

    # Direct jobs file path — from config.yaml (optional)
    # When set, uses this path directly; overrides --lang and jobs_dir defaults
    jobs: Path | None

    # Voice presets — every bundled Edge TTS voice plus config.yaml's `voices:`
    # section on top (config.yaml wins on name collisions), already flattened
    # into {alias: {tts_server, voice_name, ...}}. See batch/voices.py.
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


def load(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    cfg = shared_config.load(config_path, env_path)
    sec = cfg.section(SECTION)

    def _resolve(value: str) -> Path:
        return cfg.resolve(value)

    # `langs:` is shared with the pilot stage; the batch stage only needs the
    # file suffix out of each entry.
    langs: dict[str, dict] = {
        code: {"file_suffix": str(entry["file_suffix"])} for code, entry in cfg.langs().items()
    }

    jobs_dir = cfg.path_value(SECTION, "jobs_dir")

    # Direct jobs path (optional) — overrides langs/jobs_dir defaults
    cfg_jobs = sec.get("jobs")
    jobs = _resolve(str(cfg_jobs)) if cfg_jobs else None

    exports_default = cfg.path_value(SECTION, "exports_dir", "./exports")
    assert exports_default is not None  # a default was supplied

    s = Settings(
        api_url=str(cfg.require(SECTION, "api_url")).rstrip("/"),
        mpt_storage=_resolve(str(cfg.require(SECTION, "mpt_storage"))),
        mpt_songs_dir=_resolve(str(cfg.require(SECTION, "mpt_songs_dir"))),
        output_dir=_resolve(str(sec["output_dir"])) if sec.get("output_dir") else exports_default,
        seen_file=_resolve(str(sec.get("seen_file", "./seen.txt"))),
        langs=langs,
        jobs_dir=jobs_dir,
        jobs=jobs,
        voice_pool=voices.build_full_pool(sec.get("voices", {})),
        log_file=_resolve(str(sec.get("log_file", "./logs/batch.log"))),
        log_max_bytes=int(sec.get("log_max_mb", 10)) * 1024 * 1024,
        max_wait_seconds=int(sec.get("max_wait_seconds", 2400)),
        stuck_threshold_seconds=int(sec.get("stuck_threshold_seconds", 3600)),
        max_retries=int(sec.get("max_retries", 3)),
        retry_delay_seconds=int(sec.get("retry_delay_seconds", 180)),
        max_consecutive_failures=int(sec.get("max_consecutive_failures", 3)),
        cache_cleanup_enabled=(lambda v: v is True or v == 1)(
            sec.get("cache_cleanup_enabled", True)
        ),
        cache_cleanup_interval=int(sec.get("cache_cleanup_interval", 6)),
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_prefix=cfg.telegram_prefix(SECTION),
    )
    validate(s)
    return s


def validate(s: Settings) -> None:
    """Warn about likely configuration mistakes (does not raise)."""
    if not s.mpt_storage.exists():
        print(
            f"[{s.telegram_prefix}] WARNING: mpt_storage '{s.mpt_storage}' does not exist — "
            "API file paths and fallback lookups will fail"
        )
    if not s.api_url.startswith("http"):
        print(
            f"[{s.telegram_prefix}] WARNING: api_url '{s.api_url}' doesn't start with http — "
            "may not work"
        )
