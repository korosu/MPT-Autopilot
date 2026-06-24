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

_ROOT = Path(__file__).parent.parent


@dataclass
class LangSettings:
    label: str
    file_suffix: str
    voice_rate_min: float
    voice_rate_max: float
    voices: list[str]
    job_defaults: dict[str, Any]


@dataclass
class Settings:
    # LLM — from .env
    api_key: str
    base_url: str
    model: str

    # Generation — from config.yaml
    generate_count: int
    refill_threshold: int
    scan_dirs: list[str]
    langs: dict[str, LangSettings]

    @property
    def is_anthropic(self) -> bool:
        return "anthropic.com" in self.base_url

    def lang(self, code: str) -> LangSettings:
        if code not in self.langs:
            raise ValueError(
                f"Unknown lang '{code}'. "
                f"Available in config.yaml: {list(self.langs)}"
            )
        return self.langs[code]


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise EnvironmentError(
            f"'{key}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return val


def load(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or (_ROOT / ".env"))

    cfg_path = config_path or (_ROOT / "config.yaml")
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found: {cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    gen = raw.get("generation", {})
    langs_raw = raw.get("langs", {})
    scan_dirs = raw.get("scan_dirs") or []

    langs: dict[str, LangSettings] = {}
    for code, lr in langs_raw.items():
        langs[code] = LangSettings(
            label=lr.get("label", code.upper()),
            file_suffix=lr.get("file_suffix", ""),
            voice_rate_min=float(lr.get("voice_rate_min", 1.05)),
            voice_rate_max=float(lr.get("voice_rate_max", 1.20)),
            voices=lr.get("voices", []),
            job_defaults=lr.get("job_defaults", {}),
        )

    return Settings(
        api_key=_require_env("LLM_API_KEY"),
        base_url=_require_env("LLM_BASE_URL").rstrip("/"),
        model=_require_env("LLM_MODEL"),
        generate_count=int(gen.get("count", 21)),
        refill_threshold=int(gen.get("threshold", 10)),
        scan_dirs=scan_dirs,
        langs=langs,
    )
