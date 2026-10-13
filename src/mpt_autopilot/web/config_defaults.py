"""
web/config_defaults.py — the values "Fill from config" writes into the form.

The page's controls are generated from argparse, whose defaults are code
constants ("or 5", "or the current directory"). This module answers the other
question: what does *this* config.yaml say the stage would actually use?

It mirrors the stages' own resolution order rather than reimplementing it, and
every entry names the line it mirrors — a second, silently different resolution
would make the button lie about what a run will do. Only config-supplied values
are offered: a key the config never sets leaves the field empty (the input's
placeholder already shows the argparse default), so every filled value traces
back to a line the user can edit.

Nothing here may raise. The page renders for a config with whole sections
missing, so every read is defensive and a malformed value is ignored rather
than guessed at.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from mpt_autopilot import config as shared_config


def _pilot_generation(cfg: shared_config.Config) -> dict:
    # pilot/settings.py:158 — `generation` is optional and may be absent.
    generation = cfg.section("pilot").get("generation")
    return generation if isinstance(generation, dict) else {}


def _as_path(value: Path | None) -> str | None:
    return None if value is None else str(value)


def _as_int(value: object) -> str | None:
    # bool is an int subclass and True is not a count; a float or a list is a
    # config typo, not a number to truncate.
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        return str(int(value))
    except ValueError:
        return None


def _as_list(value: object) -> str | None:
    """A list setting as the comma-separated text the form's input expects."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        items = [str(item) for item in value if str(item).strip()]
        return ", ".join(items) or None
    return None


def _compact(mapping: Mapping[str, str | None]) -> dict[str, str]:
    return {dest: value for dest, value in mapping.items() if value is not None}


def _pilot_dirs(cfg: shared_config.Config) -> dict[str, str | None]:
    # pilot/settings.py:184-185 — path_value already prefers the stage's own
    # key and falls back to the shared `paths:` block; seen_dir then falls back
    # to jobs_dir. init-seen reads the same settings.seen_dir (init_seen.py:218).
    return {
        "jobs_dir": _as_path(cfg.path_value("pilot", "jobs_dir")),
        "seen_dir": _as_path(
            cfg.path_value("pilot", "seen_dir") or cfg.path_value("pilot", "jobs_dir")
        ),
    }


def _refill(cfg: shared_config.Config) -> dict[str, str]:
    generation = _pilot_generation(cfg)
    return _compact(
        {
            **_pilot_dirs(cfg),
            # pilot/settings.py:245-246 — the code defaults (21, 10) stay
            # unfilled: only what the config says is a config value.
            "count": _as_int(generation.get("count")),
            "threshold": _as_int(generation.get("threshold")),
        }
    )


def _init_seen(cfg: shared_config.Config) -> dict[str, str]:
    return _compact(
        {
            # pilot/settings.py:159 — scan_dirs, the directories init-seen adds
            # to the ones passed on the command line (init_seen.py:223).
            "dirs": _as_list(cfg.section("pilot").get("scan_dirs")),
            **_pilot_dirs(cfg),
        }
    )


def _batch(cfg: shared_config.Config) -> dict[str, str]:
    # batch/run.py:1026-1034 — a direct `batch.jobs` wins, otherwise the jobs
    # directory plus the plain jobs.yaml when no --lang narrows it. The child
    # runs with cwd set to the config's directory, so the derived path is
    # resolved here the same way.
    jobs_dir = cfg.path_value("batch", "jobs_dir")
    configured_jobs = cfg.section("batch").get("jobs")
    jobs = (
        cfg.resolve(str(configured_jobs))
        if configured_jobs
        else (jobs_dir / "jobs.yaml" if jobs_dir is not None else None)
    )
    # batch/run.py:436-446 + seen.py:55-66 — the seen directory plus the plain
    # seen.txt (a --lang would switch it to seen_<lang>.txt).
    seen_dir = cfg.path_value("batch", "seen_dir")
    return _compact(
        {
            "jobs": _as_path(jobs),
            "seen": _as_path(seen_dir / "seen.txt" if seen_dir is not None else None),
        }
    )


def _enrich(cfg: shared_config.Config) -> dict[str, str]:
    section = cfg.section("enricher")
    platform = section.get("platform")
    return _compact(
        {
            # enricher/settings.py:259
            "dir": _as_path(cfg.path_value("enricher", "videos_dir")),
            # enricher/settings.py:124-130 — lower-cased there, so match it.
            "platform": str(platform).lower() if isinstance(platform, str) and platform else None,
            # enricher/settings.py:265
            "batch_size": _as_int(section.get("batch_size")),
        }
    )


def _upload(cfg: shared_config.Config) -> dict[str, str]:
    configured = cfg.section("uploader").get("accounts_file")
    # uploader/settings.py:100-105 — only the configured spelling is a config
    # value; the "sits next to config.yaml" fallback is a code default, so it
    # stays unfilled like every other default.
    return _compact(
        {
            "accounts_file": _as_path(cfg.resolve(str(configured))) if configured else None,
        }
    )


def _run(cfg: shared_config.Config) -> dict[str, str]:
    # pipeline.py's --refill-count / --refill-threshold pass straight through
    # to the refill stage, so they read the same generation keys.
    generation = _pilot_generation(cfg)
    return _compact(
        {
            "refill_count": _as_int(generation.get("count")),
            "refill_threshold": _as_int(generation.get("threshold")),
        }
    )


def stage_defaults(cfg: shared_config.Config) -> dict[str, dict[str, str]]:
    """stage name → flag dest → the value config.yaml implies for it."""
    return {
        "refill": _refill(cfg),
        "init-seen": _init_seen(cfg),
        "batch": _batch(cfg),
        "enrich": _enrich(cfg),
        "upload": _upload(cfg),
        "run": _run(cfg),
        # validate registers no flag the config speaks to.
    }
