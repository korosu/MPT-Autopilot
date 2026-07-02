"""
generator/jobs.py

All YAML I/O for jobs_<lang>.yaml files.

Reading  → yaml.safe_load (standard, reliable)
Writing  → append-only: new jobs are serialised to text manually and
           appended to the file as-is. This preserves 100% of the
           original file's style (quotes, blank lines, indentation)
           because we never touch what's already there.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from shorts_pilot.generator.lock import file_lock

# Canonical key order for serialised job entries — matches hand-written style.
_KEY_ORDER = [
    "name", "enabled", "output_file", "video_subject",
    "video_clip_duration", "video_concat_mode", "voice_rate",
    "voice_name", "bgm_type", "bgm_volume", "paragraph_number",
]

# ── Reading ───────────────────────────────────────────────────────────────────

def _path(jobs_dir: Path, lang: str) -> Path:
    return jobs_dir / f"jobs_{lang}.yaml"


def load(jobs_dir: Path, lang: str) -> dict[str, Any]:
    p = _path(jobs_dir, lang)
    if not p.exists():
        raise FileNotFoundError(
            f"Jobs file not found: {p}\n"
            f"Create jobs_{lang}.yaml in your jobs directory first."
        )
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # An empty or comment-only file parses to None; a malformed one could
    # parse to a non-dict. Normalise to the expected shape so downstream
    # callers (existing_names_from, count_pending_from) don't crash on
    # cfg.get(...).
    return data if isinstance(data, dict) else {"jobs": []}


def existing_names_from(cfg: dict[str, Any]) -> set[str]:
    """Return output_file values from an already-loaded config dict."""
    return {job.get("output_file", "") for job in (cfg.get("jobs") or [])}


def existing_names(jobs_dir: Path, lang: str) -> set[str]:
    return existing_names_from(load(jobs_dir, lang))


def count_pending_from(cfg: dict[str, Any], seen: set[str]) -> int:
    """Count pending jobs from an already-loaded config dict."""
    return sum(
        1 for job in (cfg.get("jobs") or [])
        if job.get("enabled", True)
        and job.get("output_file", "") not in seen
    )


def count_pending(jobs_dir: Path, lang: str, seen: set[str]) -> int:
    """
    Count jobs that are:
      - enabled: true  (missing key → treated as True)
      - output_file NOT in seen

    This is the real queue depth.
    """
    return count_pending_from(load(jobs_dir, lang), seen)


# ── Writing ───────────────────────────────────────────────────────────────────

def _scalar(value: Any) -> str:
    """
    Render a scalar value the way the original jobs files look:
    - strings  → double-quoted  "value"
    - booleans → unquoted lowercase  true / false
    - numbers  → unquoted
    - None     → empty string ""
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    # String — double-quote and escape backslashes, double-quotes, and
    # control characters (a stray literal newline from the LLM's JSON
    # response must not become a raw newline in the appended block).
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _job_to_yaml(job: dict[str, Any]) -> str:
    """
    Serialise one job dict to the YAML block style used in jobs files.
    Keys are written in _KEY_ORDER; unknown keys follow after.
    """
    ordered: dict[str, Any] = {k: job[k] for k in _KEY_ORDER if k in job}
    ordered.update({k: v for k, v in job.items() if k not in ordered})

    lines: list[str] = []
    for i, (key, value) in enumerate(ordered.items()):
        prefix = "  - " if i == 0 else "    "
        lines.append(f"{prefix}{key}: {_scalar(value)}")
    return "\n".join(lines)


def append(jobs_dir: Path, lang: str, new_jobs: list[dict[str, Any]]) -> None:
    """
    Append new_jobs to the end of jobs_<lang>.yaml.

    The existing file content is never modified — new entries are
    written as raw text after the last line, preserving original style.
    """
    p = _path(jobs_dir, lang)

    # Hold a lock across the read+append so two concurrent refill runs
    # (e.g. two langs, or a retry racing a previous run) can't interleave
    # writes to the same file.
    with file_lock(p):
        # Read once: check trailing newline, then keep handle open for appending.
        content = p.read_text(encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            for job in new_jobs:
                f.write("\n")
                f.write(_job_to_yaml(job))
                f.write("\n")


# ── Utilities ─────────────────────────────────────────────────────────────────

def safe_name(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")[:60]
