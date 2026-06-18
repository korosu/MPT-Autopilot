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
        self.max_tags: int = int(cfg.get("max_tags", 15))
        self.always_include: list[str] = cfg.get("always_include", ["#shorts"])

        # Prompts
        self.prompt_detect_language: str = cfg["prompt_detect_language"]
        self.prompt_generate: str = cfg["prompt_generate"]

        # Logging
        self.log_dir: Path = _ROOT / "logs"
        self.log_file: Path = self.log_dir / "enricher.log"
        self.max_log_size: int = 5 * 1024 * 1024  # 5 MB


# Singleton — imported everywhere as `from enricher.config import settings`
settings = Settings()
