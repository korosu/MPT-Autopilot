"""
config.py — loads .env and config.yaml, exposes a single Settings object.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Resolve paths relative to the project root (parent of this package)
_ROOT = Path(__file__).parent.parent
_ENV_FILE = _ROOT / ".env"
_CONFIG_FILE = _ROOT / "config.yaml"

load_dotenv(_ENV_FILE)


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
            f"Check your config against .env.example."
        )
    return cfg[key]


def _load_yaml() -> dict:
    if not _CONFIG_FILE.exists():
        raise FileNotFoundError(f"config.yaml not found at {_CONFIG_FILE}")
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class Settings:
    def __init__(self) -> None:
        cfg = _load_yaml()

        # LLM connection
        self.api_key: str = _require_env("LLM_API_KEY")
        self.base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")

        # Tag generation
        self.min_tags: int = int(cfg.get("min_tags", 12))
        self.max_tags: int = int(cfg.get("max_tags", 20))
        if self.min_tags >= self.max_tags:
            raise ValueError(
                f"config.yaml: min_tags ({self.min_tags}) must be less than "
                f"max_tags ({self.max_tags})."
            )
        self.always_include: list[str] = cfg.get("always_include", ["#shorts"])

        # Prompts — required keys; use _require_cfg for actionable error messages
        self.prompt_detect_language: str = _require_cfg(cfg, "prompt_detect_language")
        self.prompt_generate: str = _require_cfg(cfg, "prompt_generate")

        # Temperature support — explicit opt-out in config.yaml overrides heuristic
        self.supports_temperature: bool = cfg.get("supports_temperature", True)

        # Logging
        self.log_dir: Path = _ROOT / "logs"
        self.log_file: Path = self.log_dir / "enricher.log"
        self.max_log_size: int = 5 * 1024 * 1024  # 5 MB


# Singleton — instantiated lazily so tests can import modules without a real .env
_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


# Convenience attribute — behaviour-compatible with the old `settings = Settings()`
# but now deferred until first access.
class _LazySettings:
    """Proxy that instantiates Settings on first attribute access."""

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(_get_settings(), name)


settings: Settings = _LazySettings()  # type: ignore[assignment]
