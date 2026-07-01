"""
config.py — loads .env and config.yaml, exposes a single Settings object.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from hashtag_enricher.enricher.postprocess import PLATFORM_HARD_LIMITS

_ROOT = Path.cwd()

load_dotenv(_ROOT / ".env")


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Copy .env.example to .env and fill in your values."
        )
    return value


def _require_cfg(cfg: dict, key: str) -> str:
    if key not in cfg:
        raise KeyError(
            f"config.yaml is missing required key '{key}'. "
            f"Check your config.yaml against the defaults in config.yaml.example."
        )
    return cfg[key]


def _load_yaml() -> dict:
    config_file = _ROOT / "config.yaml"
    if not config_file.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_file}\n"
            f"Copy config.yaml.example to config.yaml and adjust as needed."
        )
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class Settings:
    def __init__(self) -> None:
        cfg = _load_yaml()

        # ── LLM connection ────────────────────────────────────────────────────
        self.api_key: str = _require_env("LLM_API_KEY")
        self.base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")

        # ── Platform ──────────────────────────────────────────────────────────
        self.platform: str = cfg.get("platform", "youtube").lower()
        valid_platforms = set(PLATFORM_HARD_LIMITS.keys())
        if self.platform not in valid_platforms:
            raise ValueError(
                f"config.yaml: platform must be one of {sorted(valid_platforms)}, "
                f"got '{self.platform}'"
            )

        # Hard limit imposed by the platform (not user-configurable)
        self.hard_limit: int = PLATFORM_HARD_LIMITS[self.platform]

        # ── Tag count ────────────────────────────────────────────────────────
        self.min_tags: int = int(cfg.get("min_tags", 3))
        self.max_tags: int = int(cfg.get("max_tags", 5))

        if self.min_tags < 1:
            raise ValueError(f"config.yaml: min_tags ({self.min_tags}) must be at least 1.")
        if self.min_tags >= self.max_tags:
            raise ValueError(
                f"config.yaml: min_tags ({self.min_tags}) must be less than "
                f"max_tags ({self.max_tags})."
            )
        if self.max_tags > self.hard_limit:
            raise ValueError(
                f"config.yaml: max_tags ({self.max_tags}) exceeds the "
                f"{self.platform} limit of {self.hard_limit}. "
                f"Lower max_tags to {self.hard_limit} or less."
            )

        # ── Tag quality filters ───────────────────────────────────────────────
        self.max_tag_length: int = int(cfg.get("max_tag_length", 20))
        if self.max_tag_length < 2:
            raise ValueError(
                f"config.yaml: max_tag_length ({self.max_tag_length}) must be at least 2."
            )

        raw_banned: list[str] = cfg.get("banned_tags", [])
        self.banned_tags: frozenset[str] = frozenset(t.lower() for t in raw_banned)

        # ── Always-include tags ───────────────────────────────────────────────
        self.always_include: list[str] = cfg.get("always_include", ["#shorts"])

        # ── Prompts ───────────────────────────────────────────────────────────
        self.prompt_detect_language: str = _require_cfg(cfg, "prompt_detect_language")
        self.prompt_generate: str = _require_cfg(cfg, "prompt_generate")
        self.prompt_detect_and_generate: str = _require_cfg(cfg, "prompt_detect_and_generate")

        # ── Temperature support ───────────────────────────────────────────────
        # Set to false for reasoning models (o1, o3, o4-mini) that reject temperature.
        self.supports_temperature: bool = cfg.get("supports_temperature", True)

        # ── Logging ───────────────────────────────────────────────────────────
        self.log_dir: Path = _ROOT / "logs"
        self.log_file: Path = self.log_dir / "enricher.log"
        self.max_log_size: int = 5 * 1024 * 1024  # 5 MB


# ── Lazy singleton ────────────────────────────────────────────────────────────

_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


class _LazySettings:
    """Proxy that instantiates Settings on first attribute access."""

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(_get_settings(), name)


settings: Settings = _LazySettings()  # type: ignore[assignment]
