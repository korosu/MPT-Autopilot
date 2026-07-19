"""
generator/settings.py

Loads .env (API credentials) and config.yaml (generation settings)
into a single Settings object used across the package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ROOT = Path.cwd()


@dataclass
class LangSettings:
    label: str
    file_suffix: str
    voice_rate_min: float
    voice_rate_max: float
    voices: list[str]
    job_defaults: dict[str, Any]
    theme_list: list[str]


@dataclass
class Settings:
    # LLM — from .env
    api_key: str
    base_url: str
    model: str

    # Telegram alerts — from .env (optional)
    telegram_token: str
    telegram_chat_id: str

    # Generation — from config.yaml
    generate_count: int
    refill_threshold: int
    scan_dirs: list[str]
    langs: dict[str, LangSettings]

    # Paths — from config.yaml (optional; CLI flags always take priority)
    jobs_dir: Path | None
    seen_dir: Path | None

    @property
    def is_anthropic(self) -> bool:
        return "anthropic.com" in self.base_url

    def lang(self, code: str) -> LangSettings:
        if code not in self.langs:
            raise ValueError(f"Unknown lang '{code}'. Available in config.yaml: {list(self.langs)}")
        return self.langs[code]


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise OSError(f"'{key}' is not set. Copy .env.example to .env and fill in your values.")
    return val


def load(
    config_path: Path | None = None,
    env_path: Path | None = None,
    *,
    require_llm: bool = True,
) -> Settings:
    load_dotenv(env_path or (_ROOT / ".env"))

    cfg_path = config_path or (_ROOT / "config.yaml")
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found: {cfg_path}")

    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = raw if isinstance(raw, dict) else {}

    gen = raw.get("generation") or {}
    langs_raw = raw.get("langs") or {}
    scan_dirs = raw.get("scan_dirs") or []
    paths_raw = raw.get("paths") or {}

    cfg_jobs_dir = paths_raw.get("jobs_dir")
    cfg_seen_dir = paths_raw.get("seen_dir")

    langs: dict[str, LangSettings] = {}
    for code, lr in langs_raw.items():
        lr = lr or {}  # a lang block with nothing under it (`en:` alone) → {}
        # Parse theme_list — list of strings, or a bare string (wraps to list).
        # Absent/malformed → empty list.
        tl_raw = lr.get("theme_list")
        if isinstance(tl_raw, list):
            theme_list = [str(t).strip() for t in tl_raw if str(t).strip()]
        elif isinstance(tl_raw, str) and tl_raw.strip():
            theme_list = [tl_raw.strip()]
        else:
            theme_list = []
        if tl_raw is not None and not theme_list:
            print(f"[warn] lang '{code}' theme_list is empty or malformed — ignoring")
        langs[code] = LangSettings(
            label=lr.get("label", code.upper()),
            file_suffix=lr.get("file_suffix", ""),
            voice_rate_min=float(lr.get("voice_rate_min", 1.05)),
            voice_rate_max=float(lr.get("voice_rate_max", 1.20)),
            voices=lr.get("voices", []),
            job_defaults=lr.get("job_defaults", {}),
            theme_list=theme_list,
        )

    # Resolve relative paths against the config.yaml location, not cwd,
    # so it behaves the same regardless of where the command is run from.
    cfg_dir = cfg_path.resolve().parent
    jobs_dir = (cfg_dir / cfg_jobs_dir).resolve() if cfg_jobs_dir else None
    seen_dir = (cfg_dir / cfg_seen_dir).resolve() if cfg_seen_dir else jobs_dir

    def _env(key: str) -> str:
        if require_llm:
            return _require_env(key)
        return os.environ.get(key, "").strip()

    return Settings(
        api_key=_env("LLM_API_KEY"),
        base_url=_env("LLM_BASE_URL").rstrip("/"),
        model=_env("LLM_MODEL"),
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        generate_count=int(gen.get("count", 21)),
        refill_threshold=int(gen.get("threshold", 10)),
        scan_dirs=scan_dirs,
        langs=langs,
        jobs_dir=jobs_dir,
        seen_dir=seen_dir,
    )
