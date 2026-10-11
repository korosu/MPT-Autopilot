"""
pilot/settings.py

Reads the `pilot:` section of the shared config.yaml (plus the shared `langs:`
and `paths:` blocks) and the LLM/Telegram credentials from .env into a single
Settings object used across the pilot stage.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mpt_autopilot import config as shared_config
from mpt_autopilot.config import ConfigError, warn_cleartext

SECTION = "pilot"

# Same set the enricher validates against — one setting, one meaning. A typo
# used to reach the provider verbatim inside chat_template_kwargs.
VALID_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})

_logger = logging.getLogger(__name__)


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
    seen_max_mb: int
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

    # R1-step2/A-3: ask the pilot's LLM for explicit visual search terms per
    # idea, in the same response (one call). Off by default — it spends prompt
    # budget and tokens, and until the pilot wrote video_terms they were
    # dropped anyway (B-7).
    visual_terms_enabled: bool = False
    # Required in config.yaml when visual_terms is true (the rules are content,
    # not code — lesson B-10: an inline prompt nobody sees degrades silently).
    visual_terms_rules: str = ""

    # R4: sampling control. None = don't send the key at all (reasoning models
    # reject temperature; default keeps byte-for-byte behavior). A number is
    # sent to both transports verbatim.
    temperature: float | None = None

    # R4: ask OpenAI-compatible providers for a guaranteed JSON object
    # (response_format) instead of relying on the tolerant parser. Off by
    # default: many providers reject the field, and the parser already
    # salvages near-JSON output.
    strict_json: bool = False

    @property
    def is_anthropic(self) -> bool:
        """
        True when base_url points at Anthropic's API.

        Matched against the host, not the whole URL: a substring test also fired
        for an unrelated endpoint carrying the name anywhere in its path or
        query (`https://proxy.example/?upstream=anthropic.com`), and the answer
        decides which request body shape llm.py builds.
        """
        host = urlsplit(self.base_url).hostname or ""
        return host == "anthropic.com" or host.endswith(".anthropic.com")

    def lang(self, code: str) -> LangSettings:
        if code not in self.langs:
            raise ValueError(f"Unknown lang '{code}'. Available in config.yaml: {list(self.langs)}")
        return self.langs[code]


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ConfigError(f"'{key}' is not set. Copy .env.example to .env and fill in your values.")
    return val


def _as_voice_list(code: str, value: object) -> list[str]:
    """
    Coerce langs.<code>.voices to a list of strings.

    A bare scalar (`voices: gemini:puck`) used to pass through untouched;
    `voices[0]` then yielded "g" and `out.get("voice_name") not in voices`
    became a substring test.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(
            f"config.yaml: langs.{code}.voices must be a list of voice names, "
            f"got {type(value).__name__}"
        )
    return [str(v) for v in value]


def _as_job_defaults(code: str, value: object) -> dict[str, Any]:
    """Coerce langs.<code>.job_defaults to a mapping — every default is read by key."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"config.yaml: langs.{code}.job_defaults must be a mapping, got {type(value).__name__}"
        )
    return value


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
            _logger.warning("lang '%s' theme_list is empty or malformed — ignoring", code)
        langs[code] = LangSettings(
            label=lr["label"],
            file_suffix=lr["file_suffix"],
            voice_rate_min=float(lr.get("voice_rate_min", 1.05)),
            voice_rate_max=float(lr.get("voice_rate_max", 1.20)),
            voices=_as_voice_list(code, lr.get("voices")),
            job_defaults=_as_job_defaults(code, lr.get("job_defaults")),
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
    reasoning_effort = str(gen.get("reasoning_effort", "medium")).lower()
    if reasoning_effort not in VALID_REASONING_EFFORTS:
        raise ValueError(
            f"config.yaml: pilot.generation.reasoning_effort must be one of "
            f"{sorted(VALID_REASONING_EFFORTS)}, got '{reasoning_effort}'"
        )
    reasoning_max_tokens = int(gen.get("reasoning_max_tokens", 8192))

    # visual_terms (R1-step2/A-3): rules are required when the feature is on.
    visual_terms_enabled = bool(gen.get("visual_terms", False))
    visual_terms_rules = ""
    if visual_terms_enabled:
        rules = gen.get("visual_terms_rules")
        if not isinstance(rules, str) or not rules.strip():
            raise ValueError(
                "config.yaml: pilot.generation.visual_terms_rules is required "
                "when pilot.generation.visual_terms is true — the term rules "
                "are content, not code (see docs/recommend/, A-3)"
            )
        visual_terms_rules = rules

    # temperature (R4): tri-state like reasoning_enabled — absent means the
    # key is not sent at all.
    temperature_raw = gen.get("temperature")
    if temperature_raw is not None and (
        isinstance(temperature_raw, bool) or not isinstance(temperature_raw, (int, float))
    ):
        raise ValueError(
            f"config.yaml: pilot.generation.temperature must be a number "
            f"(or omitted entirely) — got {temperature_raw!r}"
        )
    temperature = float(temperature_raw) if temperature_raw is not None else None
    strict_json = bool(gen.get("strict_json", False))

    s = Settings(
        api_key=_env("LLM_API_KEY"),
        base_url=_env("LLM_BASE_URL").rstrip("/"),
        model=_env("LLM_MODEL"),
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        telegram_prefix=cfg.telegram_prefix(SECTION),
        generate_count=int(gen.get("count", 21)),
        refill_threshold=int(gen.get("threshold", 10)),
        seen_max_mb=int(sec.get("seen_max_mb", 64)),
        scan_dirs=scan_dirs,
        langs=langs,
        jobs_dir=jobs_dir,
        seen_dir=seen_dir,
        reasoning_enabled=reasoning_enabled,
        reasoning_effort=reasoning_effort,
        reasoning_max_tokens=reasoning_max_tokens,
        visual_terms_enabled=visual_terms_enabled,
        visual_terms_rules=visual_terms_rules,
        temperature=temperature,
        strict_json=strict_json,
    )
    warn_cleartext(s.base_url, "LLM_BASE_URL")
    return s
