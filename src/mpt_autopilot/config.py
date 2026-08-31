"""
config.py — the single config.yaml loader shared by every MPT Autopilot stage.

The four merged tools each had their own `config.yaml`. Here there is exactly
one file, split into per-stage sections plus a small shared part:

    telegram_prefix: "mpt-autopilot"   # shared default; a section may override
    paths:    { jobs_dir, seen_dir, exports_dir, videos_dir }
    langs:    { <code>: { label, file_suffix, ... } }
    pilot:    { ... }
    batch:    { ... }
    enricher: { ... }
    uploader: { ... }
    pipeline: { ... }

`langs` is deliberately shared: the batch stage only reads `file_suffix` from
it while the pilot stage reads the whole block, and keeping two copies in sync
was a real source of broken multi-language runs before the merge.

Relative paths inside config.yaml resolve against the config file's own
location — never the current working directory — so a command behaves the same
no matter where it is run from. That was the behaviour of all four original
loaders and it is preserved here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_NAME = "config.yaml"
DEFAULT_ENV_NAME = ".env"

# Sections a stage may ask for. Kept explicit so a typo raises instead of
# silently handing back an empty dict.
SECTIONS = ("pilot", "batch", "enricher", "uploader", "pipeline")

_SHARED_PREFIX_DEFAULT = "mpt-autopilot"


class ConfigError(Exception):
    """config.yaml is missing, unreadable, or structurally wrong."""


@dataclass
class Config:
    """A parsed config.yaml plus the helpers stages need to read it."""

    path: Path
    raw: dict = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return self.path.resolve().parent

    # ── path resolution ───────────────────────────────────────────────────────

    def resolve(self, value: str | Path) -> Path:
        """Resolve a config value as a path relative to config.yaml's directory."""
        return (self.dir / Path(value)).expanduser().resolve()

    # ── sections ──────────────────────────────────────────────────────────────

    def section(self, name: str) -> dict:
        """
        Return one stage's section as a dict. A missing section yields `{}` so a
        stage relying purely on defaults still works; a section present but not
        a mapping is a hard error.
        """
        if name not in SECTIONS:
            raise ValueError(f"unknown config section '{name}'; expected one of {list(SECTIONS)}")
        value = self.raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(
                f"{self.path.name}: section '{name}' must be a mapping, "
                f"got {type(value).__name__}. Compare against config.example.yaml."
            )
        return value

    def require(self, section: str, key: str) -> object:
        """Read a required key from a section, with a message that names both."""
        sec = self.section(section)
        if key not in sec:
            raise ConfigError(
                f"{self.path.name} is missing required key '{section}.{key}'. "
                f"Check your {self.path.name} against config.example.yaml."
            )
        return sec[key]

    # ── shared values ─────────────────────────────────────────────────────────

    def telegram_prefix(self, section: str) -> str:
        """
        The alert prefix for one stage: the section's own `telegram_prefix` if it
        sets one, otherwise the top-level shared value.
        """
        own = self.section(section).get("telegram_prefix")
        if own:
            return str(own)
        return str(self.raw.get("telegram_prefix", _SHARED_PREFIX_DEFAULT))

    def paths(self) -> dict:
        raw_paths = self.raw.get("paths")
        if raw_paths is None:
            return {}
        if not isinstance(raw_paths, dict):
            raise ConfigError(f"{self.path.name}: 'paths' must be a mapping")
        return raw_paths

    def path_value(self, section: str, key: str, default: str | None = None) -> Path | None:
        """
        Resolve a path setting, preferring the stage's own value and falling
        back to the shared `paths:` block, then to `default`.

        Returns None when nothing is configured and no default is given, so a
        caller can keep its own "use the current directory" behaviour.
        """
        own = self.section(section).get(key)
        if own:
            return self.resolve(str(own))
        shared = self.paths().get(key)
        if shared:
            return self.resolve(str(shared))
        if default is not None:
            return self.resolve(default)
        return None

    def langs(self) -> dict[str, dict]:
        """
        The shared `langs:` block, normalised to `{code: {...}}` with
        `file_suffix` always present (defaulting to `_<code>`, matching the
        batch stage's previous behaviour).
        """
        raw_langs = self.raw.get("langs") or {}
        if not isinstance(raw_langs, dict):
            raise ConfigError(f"{self.path.name}: 'langs' must be a mapping of language codes")
        out: dict[str, dict] = {}
        for code, entry in raw_langs.items():
            entry = entry or {}
            if not isinstance(entry, dict):
                raise ConfigError(f"{self.path.name}: langs.{code} must be a mapping")
            merged = dict(entry)
            merged.setdefault("file_suffix", f"_{code}")
            merged.setdefault("label", str(code).upper())
            out[str(code)] = merged
        return out


# ── loading ───────────────────────────────────────────────────────────────────

_cache: dict[str, Config] = {}


def resolve_path(config_path: Path | None = None) -> Path:
    return Path(config_path) if config_path is not None else Path(DEFAULT_CONFIG_NAME)


def load(
    config_path: Path | None = None,
    env_path: Path | None = None,
    *,
    reload: bool = False,
) -> Config:
    """
    Load config.yaml (and .env alongside it) once per path.

    `.env` is looked for next to config.yaml first, then in the current
    directory, so both `mpt --config /srv/mpt/config.yaml ...` and a plain
    `mpt ...` from a checkout pick up credentials.
    """
    path = resolve_path(config_path)
    key = str(path.resolve()) if path.exists() else str(path)
    if not reload and key in _cache:
        return _cache[key]

    if not path.exists():
        raise ConfigError(
            f"{path} not found.\n"
            f"Copy config.example.yaml to {path.name} and adjust it "
            f"(see docs/migration.md if you are coming from the separate tools)."
        )

    if env_path is not None:
        load_dotenv(env_path)
    else:
        beside = path.resolve().parent / DEFAULT_ENV_NAME
        if beside.exists():
            load_dotenv(beside)
        else:
            load_dotenv(Path(DEFAULT_ENV_NAME))

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping of sections")

    cfg = Config(path=path, raw=raw)
    _cache[key] = cfg
    return cfg


def clear_cache() -> None:
    """Drop cached configs — used by tests and by `mpt run` between languages."""
    _cache.clear()
