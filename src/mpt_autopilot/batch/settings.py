"""
batch/settings.py

Reads the `batch:` section of the shared config.yaml (plus the shared `langs:`
and `paths:` blocks) and the Telegram credentials from .env into a single
Settings object used across the batch stage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from mpt_autopilot import config as shared_config
from mpt_autopilot.batch import voices
from mpt_autopilot.config import ConfigError, warn_cleartext
from mpt_autopilot.mpt_config import extract_upload_post, load_mpt_config

SECTION = "batch"


@dataclass
class Settings:
    # MoneyPrinterTurbo connection — from config.yaml
    api_url: str
    mpt_storage: Path
    mpt_songs_dir: Path

    # Output — from config.yaml
    output_dir: Path

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
    # Delay between two polls of the MPT task status (api.wait_for_task).
    poll_interval_seconds: int

    # Retry behaviour — from config.yaml
    max_retries: int
    retry_delay_seconds: int
    max_consecutive_failures: int

    # Cache cleanup — from config.yaml
    cache_cleanup_enabled: bool
    cache_cleanup_interval: int

    # seen.txt rotation — from config.yaml (optional; defaults to 64 MB)
    seen_max_mb: int

    # Telegram — token/chat_id from .env (optional), prefix from config.yaml
    telegram_token: str
    telegram_chat_id: str
    telegram_prefix: str

    # Optional audio post-pass on the finished video (R5): one ffmpeg call
    # levels loudness (R128-ish -14 LUFS) and trims the trailing silence
    # Edge TTS leaves behind. Off by default — it adds an ffmpeg dependency
    # and must not silently change existing renders.
    audio_normalize: bool = False

    # MPT API key (R6): sent as the x-api-key header on every request when
    # set. The secret lives in .env (MPT_API_KEY), not in config.yaml; empty
    # means MPT runs unauthenticated, exactly as before.
    api_key: str = ""

    # MoneyPrinterTurbo's own config.toml path (optional). When set, MPT-Autopilot
    # reads upload-post credentials from there so the user does not re-enter them.
    mpt_config_path: Path | None = None

    # Upload-post configuration: either from batch.upload_post overrides in this
    # config.yaml, or from MPT's config.toml (via mpt_config_path above).
    upload_post_enabled: bool = False
    upload_post_api_key: str = ""
    upload_post_username: str = ""
    upload_post_platforms: list[str] = field(default_factory=lambda: ["youtube", "tiktok"])
    upload_post_youtube_privacy_status: str = "public"
    # upload-post's own privacy field, which applies to the non-YouTube
    # platforms (TikTok and friends). Its accepted values come from those
    # platforms' APIs, not from ours, so there is no mapping here from
    # youtube_privacy_status: the value is passed through verbatim and the
    # default preserves the behaviour this used to hard-code. None means "not
    # configured", which upload_video() refuses to combine with a non-public
    # youtube_privacy_status rather than publishing publicly anyway.
    upload_post_privacy_level: str | None = None
    # YouTube's "AI use" disclosure (containsSyntheticMedia). Was hard-coded
    # to "true" in the request (B-9/R9.6): MoneyPrinterTurbo output is
    # AI-generated so true is the right default, but the choice belongs in
    # config.yaml, not in code.
    upload_post_contains_synthetic_media: bool = True
    # YouTube's "made for kids" audience declaration (selfDeclaredMadeForKids,
    # MPT 1.3.9): upload-post.com requires an explicit declaration and its
    # default changed from an implicit "not made for kids" to an explicit
    # one — mirroring MPT's own upload_post_youtube_made_for_kids default of
    # false. A children's channel must flip this in config.yaml (or MPT's
    # config.toml via mpt.config_path).
    upload_post_made_for_kids: bool = False


# Accept both YAML true and 1 as truthy for cache_cleanup_enabled (YAML
# booleans are native but some configs use 1/0). A frozenset would collapse
# True and 1 into one member — they are equal and hash alike — so the test is
# written as an explicit comparison.
def _is_cache_cleanup_enabled(value: object) -> bool:
    """Return True when cache_cleanup_enabled is configured as a truthy value."""
    return value is True or value == 1


def _as_platform_list(value: object) -> list[str]:
    """
    Normalise upload_post.platforms to a list of names.

    A bare string (`platforms: youtube`) is wrapped, matching how
    pilot.settings treats a scalar theme_list. It used to be cast() straight to
    list[str], which hid the real consequence from the type checker: upload_post
    iterates the value, so a string went out as platform[]=y&platform[]=o&…
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        raise ConfigError(
            f"batch.upload_post.platforms must be a list of platform names "
            f"(or a single name), got {type(value).__name__}"
        )
    return [str(item) for item in value if str(item).strip()]


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

    exports_default = cfg.require_path(SECTION, "exports_dir", "./exports")

    # ── upload-post credentials ──────────────────────────────────────────────
    # Priority (high → low):
    #   1. batch.upload_post.<key> in this config.yaml
    #   2. upload_post_<key> in MPT's config.toml (via mpt.config_path)
    #   3. safe defaults (empty strings, disabled)
    mpt_section = cfg.section("mpt")
    mpt_config_path_raw = mpt_section.get("config_path")
    mpt_config_path: Path | None = (
        cfg.resolve(str(mpt_config_path_raw)) if mpt_config_path_raw else None
    )

    mpt_app = load_mpt_config(mpt_config_path)
    mpt_upload = extract_upload_post(mpt_app)

    up_sec = sec.get("upload_post") or {}
    if not isinstance(up_sec, dict):
        raise ConfigError(
            f"batch.upload_post must be a mapping, got {type(up_sec).__name__}",
        )

    # config.yaml key → MPT config.toml key. privacy_level is config.yaml-only:
    # it is MPT-Autopilot's own setting, not something MPT's [app] section has.
    _UP_KEYS = {
        "enabled": "upload_post_enabled",
        "api_key": "upload_post_api_key",
        "username": "upload_post_username",
        "platforms": "upload_post_platforms",
        "youtube_privacy_status": "upload_post_youtube_privacy_status",
        "made_for_kids": "upload_post_youtube_made_for_kids",
    }

    def _up(key: str, default: object) -> object:
        """Resolve a single upload_post key: config.yaml takes priority."""
        if key in up_sec:
            return up_sec[key]
        mpt_key = _UP_KEYS.get(key)
        if mpt_key is None:
            return default
        return mpt_upload.get(mpt_key, default)

    up_enabled = bool(_up("enabled", False))
    up_api_key = str(_up("api_key", ""))
    up_username = str(_up("username", ""))
    up_platforms = _as_platform_list(_up("platforms", ["youtube", "tiktok"]))
    up_youtube_privacy = str(_up("youtube_privacy_status", "public"))
    up_privacy_level_raw = _up("privacy_level", None)
    up_privacy_level = str(up_privacy_level_raw) if up_privacy_level_raw else None
    # config.yaml-only (not an [app] key in MPT's config.toml): _up falls to
    # the default when neither config.yaml nor MPT carries it.
    up_contains_synthetic_media = bool(_up("contains_synthetic_media", True))
    # Mirrors MPT's upload_post_youtube_made_for_kids: falls back to MPT's
    # config.toml value when config.yaml is silent (B-13's rule applies — an
    # explicitly-disabled MPT key and an absent one must stay distinguishable).
    up_made_for_kids = bool(_up("made_for_kids", False))

    s = Settings(
        api_url=str(cfg.require(SECTION, "api_url")).rstrip("/"),
        mpt_storage=_resolve(str(cfg.require(SECTION, "mpt_storage"))),
        mpt_songs_dir=_resolve(str(cfg.require(SECTION, "mpt_songs_dir"))),
        output_dir=_resolve(str(sec["output_dir"])) if sec.get("output_dir") else exports_default,
        langs=langs,
        jobs_dir=jobs_dir,
        jobs=jobs,
        voice_pool=voices.build_full_pool(sec.get("voices", {})),
        log_file=_resolve(str(sec.get("log_file", "./logs/batch.log"))),
        log_max_bytes=int(sec.get("log_max_mb", 10)) * 1024 * 1024,
        max_wait_seconds=int(sec.get("max_wait_seconds", 2400)),
        stuck_threshold_seconds=int(sec.get("stuck_threshold_seconds", 3600)),
        poll_interval_seconds=int(sec.get("poll_interval_seconds", 10)),
        max_retries=int(sec.get("max_retries", 3)),
        retry_delay_seconds=int(sec.get("retry_delay_seconds", 180)),
        max_consecutive_failures=int(sec.get("max_consecutive_failures", 3)),
        cache_cleanup_enabled=_is_cache_cleanup_enabled(sec.get("cache_cleanup_enabled", True)),
        cache_cleanup_interval=int(sec.get("cache_cleanup_interval", 6)),
        seen_max_mb=int(sec.get("seen_max_mb", 64)),
        audio_normalize=bool(sec.get("audio_normalize", False)),
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_prefix=cfg.telegram_prefix(SECTION),
        api_key=os.getenv("MPT_API_KEY", "").strip(),
        mpt_config_path=mpt_config_path,
        upload_post_enabled=bool(up_enabled),
        upload_post_api_key=up_api_key,
        upload_post_username=up_username,
        upload_post_platforms=up_platforms,
        upload_post_youtube_privacy_status=up_youtube_privacy,
        upload_post_privacy_level=up_privacy_level,
        upload_post_contains_synthetic_media=up_contains_synthetic_media,
        upload_post_made_for_kids=up_made_for_kids,
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
    warn_cleartext(s.api_url, "batch.api_url")
