"""
enricher/settings.py — reads the `enricher:` section of the shared config.yaml
and exposes it as a lazily-created singleton `settings`.

The singleton is what the rest of the enricher stage imports, and it must stay
lazy: `mpt --help` and unrelated subcommands have to work in a directory with
no config.yaml. Nothing is read until the first attribute access.

`configure(path)` lets the CLI point the singleton at a `--config` path before
anything touches it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from mpt_autopilot import config as shared_config
from mpt_autopilot.enricher.postprocess import PLATFORM_HARD_LIMITS, platform_hard_limit

SECTION = "enricher"


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise OSError(
            f"Missing required environment variable: {key}\n"
            f"Copy .env.example to .env and fill in your values."
        )
    return value


def validate_tag_budget(platform: str, max_tags: int, always_include_count: int) -> int:
    """
    Ensure max_tags + always_include_count actually fits within a platform's
    hard limit.

    always_include tags are prepended by the code on top of the LLM's
    max_tags-worth of content tags (see llm.py's _merge_always_include), so
    the two must be budgeted together — max_tags alone being under the hard
    limit is not sufficient.

    Used both at Settings() init (for the configured `platform`) and by
    enricher/run.py when `--platform` overrides the config at runtime, so the two
    call sites can never drift apart.

    Args:
        platform:              Platform name (must be a PLATFORM_HARD_LIMITS key).
        max_tags:               config.yaml's max_tags.
        always_include_count:   len(config.yaml's always_include).

    Returns:
        The platform's hard limit, on success.

    Raises:
        ValueError: if max_tags + always_include_count exceeds the platform's
            hard limit.
    """
    hard_limit = platform_hard_limit(platform)
    total = max_tags + always_include_count
    if total > hard_limit:
        raise ValueError(
            f"max_tags ({max_tags}) + always_include ({always_include_count} tag(s)) "
            f"= {total} exceeds the {platform} limit of {hard_limit}. "
            f"Lower max_tags in config.yaml, trim always_include, or choose a "
            f"different platform."
        )
    return hard_limit


class Settings:
    def __init__(self, config_path: Path | None = None) -> None:
        shared = shared_config.load(config_path)
        cfg = shared.section(SECTION)
        self.config_path: Path = shared.path

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
        self.hard_limit: int = platform_hard_limit(self.platform)

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

        # always_include is added by the code on top of max_tags-worth of LLM
        # content tags (never counted *within* min_tags/max_tags), so the two
        # budgets together still have to fit under the platform's hard limit.
        validate_tag_budget(self.platform, self.max_tags, len(self.always_include))

        # ── Prompts ───────────────────────────────────────────────────────────
        self.prompt_detect_language: str = str(shared.require(SECTION, "prompt_detect_language"))
        self.prompt_generate: str = str(shared.require(SECTION, "prompt_generate"))
        self.prompt_detect_and_generate: str = str(
            shared.require(SECTION, "prompt_detect_and_generate")
        )

        # ── Temperature support ───────────────────────────────────────────────
        # Set to false for reasoning models (o1, o3, o4-mini) that reject temperature.
        self.supports_temperature: bool = cfg.get("supports_temperature", True)

        # ── Reasoning ("thinking") support ──────────────────────────────────────
        # Controls the vLLM/SGLang/NVIDIA-NIM-style `chat_template_kwargs`
        # extension (not part of the official OpenAI API). This whole block is
        # opt-in by design: if `reasoning_enabled` is absent from config.yaml,
        # `chat_template_kwargs` is never added to the request payload, so
        # providers that don't understand that field (plain OpenAI, etc.) see
        # no change in behavior.
        #
        #   absent                     → chat_template_kwargs omitted entirely
        #   reasoning_enabled: false   → explicitly forces thinking off
        #                                (chat_template_kwargs: {"reasoning_effort": "none"});
        #                                use for models that default reasoning ON
        #                                (e.g. Inkling) and would otherwise burn
        #                                max_tokens on hidden thinking.
        #   reasoning_enabled: true    → enables thinking at `reasoning_effort`
        #                                and switches max_tokens to the larger
        #                                `reasoning_max_tokens` budget, since
        #                                hidden reasoning tokens count against it.
        self.reasoning_enabled: bool | None = cfg.get("reasoning_enabled")
        if self.reasoning_enabled is not None and not isinstance(self.reasoning_enabled, bool):
            raise ValueError(
                f"config.yaml: reasoning_enabled must be true or false (or omitted), "
                f"got {self.reasoning_enabled!r}"
            )

        _valid_reasoning_efforts = {"none", "low", "medium", "high", "xhigh", "max"}
        self.reasoning_effort: str = str(cfg.get("reasoning_effort", "medium")).lower()
        if self.reasoning_effort not in _valid_reasoning_efforts:
            raise ValueError(
                f"config.yaml: reasoning_effort must be one of "
                f"{sorted(_valid_reasoning_efforts)}, got '{self.reasoning_effort}'"
            )

        # Effective token budget used only when reasoning_enabled: true — hidden
        # reasoning tokens are drawn from the same budget as the visible answer,
        # so this needs to be far larger than the normal 512-token cap (NVIDIA's
        # own Inkling example uses 8192).
        self.reasoning_max_tokens: int = int(cfg.get("reasoning_max_tokens", 8192))
        if self.reasoning_max_tokens < 1:
            raise ValueError(
                f"config.yaml: reasoning_max_tokens ({self.reasoning_max_tokens}) "
                f"must be at least 1."
            )

        # ── Default directory ───────────────────────────────────────────────────
        # Where `mpt enrich` looks when no path argument is given.
        self.directory: Path | None = shared.path_value(SECTION, "videos_dir")

        # ── Logging ───────────────────────────────────────────────────────────
        log_dir = shared.path_value(SECTION, "log_dir", "./logs")
        assert log_dir is not None  # a default was supplied
        self.log_dir: Path = log_dir
        self.log_file: Path = self.log_dir / "enricher.log"
        self.max_log_size: int = int(cfg.get("log_max_mb", 5)) * 1024 * 1024

        # ── Telegram ───────────────────────────────────────────────────────
        self.telegram_token: str = os.getenv("TELEGRAM_TOKEN", "").strip()
        self.telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.telegram_prefix: str = shared.telegram_prefix(SECTION)


# ── Lazy singleton ────────────────────────────────────────────────────────────
#
# The enricher stage imports `settings` at module scope, so instantiation has to
# stay lazy: `mpt --help` and every unrelated subcommand must keep working in a
# directory that has no config.yaml. Nothing is read until an attribute is
# actually touched.

_settings: Settings | None = None
_config_path: Path | None = None


def configure(config_path: Path | None) -> None:
    """
    Point the singleton at a specific config.yaml. Called by the CLI before any
    enricher code runs; resets an already-created instance so `mpt run` can
    switch config between invocations in the same process.
    """
    global _settings, _config_path
    _config_path = config_path
    _settings = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings(_config_path)
    return _settings


class _LazySettings:
    """Proxy that instantiates Settings on first attribute access."""

    def __getattr__(self, name: str) -> object:
        return getattr(_get_settings(), name)


# cast, not `# type: ignore`: the proxy is deliberately duck-typed, and callers
# should still get real Settings completion and type checking.
settings: Settings = cast(Settings, _LazySettings())
