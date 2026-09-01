"""
pilot/settings.py

Reads the `pilot:` section of the shared config.yaml (plus the shared `langs:`
and `paths:` blocks) and the LLM/Telegram credentials from .env into a single
Settings object used across the pilot stage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mpt_autopilot import config as shared_config

SECTION = "pilot"


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

    # Telegram alerts — token/chat_id from .env, prefix from config.yaml
    telegram_token: str
    telegram_chat_id: str
    telegram_prefix: str

    # Generation — from config.yaml
    generate_count: int
    refill_threshold: int
    scan_dirs: list[str]
    langs: dict[str, LangSettings]

    # Paths — from config.yaml (optional; CLI flags always take priority)
    jobs_dir: Path | None
    seen_dir: Path | None

    # Reasoning-model support — from config.yaml (optional; see load() below).
    # Tri-state: None = key absent from config.yaml, don't touch the request
    # at all (byte-for-byte prior behavior). True/False = user explicitly
    # opted in/out.
    reasoning_enabled: bool | None = None
    reasoning_effort: str = "medium"
    reasoning_max_tokens: int = 8192

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
    cfg = shared_config.load(config_path, env_path)
    sec = cfg.section(SECTION)

    gen = sec.get("generation") or {}
    scan_dirs = sec.get("scan_dirs") or []

    langs: dict[str, LangSettings] = {}
    for code, lr in cfg.langs().items():
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

    jobs_dir = cfg.path_value(SECTION, "jobs_dir")
    seen_dir = cfg.path_value(SECTION, "seen_dir") or jobs_dir

    def _env(key: str) -> str:
        if require_llm:
            return _require_env(key)
        return os.environ.get(key, "").strip()

    # reasoning_enabled is deliberately read WITHOUT a default arg (gen.get(...),
    # not gen.get(..., False)) — this must stay a 3-state read (None / True /
    # False), not a 2-state bool. That's the only way to tell "key absent,
    # config not updated yet" apart from "user explicitly turned it off".
    # Absent → payload untouched downstream → identical behavior to before
    # this setting existed, for every provider that doesn't need it.
    reasoning_enabled = gen.get("reasoning_enabled")
    if reasoning_enabled is not None and not isinstance(reasoning_enabled, bool):
        raise ValueError(
            f"pilot.generation.reasoning_enabled must be true, false, or omitted "
            f"entirely — got {reasoning_enabled!r}"
        )
    reasoning_effort = str(gen.get("reasoning_effort", "medium"))
    reasoning_max_tokens = int(gen.get("reasoning_max_tokens", 8192))

    return Settings(
        api_key=_env("LLM_API_KEY"),
        base_url=_env("LLM_BASE_URL").rstrip("/"),
        model=_env("LLM_MODEL"),
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        telegram_prefix=cfg.telegram_prefix(SECTION),
        generate_count=int(gen.get("count", 21)),
        refill_threshold=int(gen.get("threshold", 10)),
        scan_dirs=scan_dirs,
        langs=langs,
        jobs_dir=jobs_dir,
        seen_dir=seen_dir,
        reasoning_enabled=reasoning_enabled,
        reasoning_effort=reasoning_effort,
        reasoning_max_tokens=reasoning_max_tokens,
    )
