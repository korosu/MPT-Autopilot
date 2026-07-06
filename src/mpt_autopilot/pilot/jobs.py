"""
generator/jobs.py

All YAML I/O for jobs_<lang>.yaml files.

Reading  → yaml.safe_load (standard, reliable)
Writing  → append-only: new jobs are serialised to text manually (never
           via yaml.dump) so 100% of the original file's style (quotes,
           blank lines, indentation) is preserved — existing content is
           never rewritten or reformatted, only added to. The resulting
           full content is written with a temp-file + os.replace() swap
           (see _atomic_write) rather than opened in append mode, so a
           concurrent reader or a second writer racing past the lock can
           never observe a corrupted, half-written file.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from shorts_pilot.generator.lock import file_lock

# Canonical key order for serialised job entries — matches hand-written style.
_KEY_ORDER = [
    "name", "enabled", "output_file", "video_subject",
    "video_clip_duration", "video_concat_mode", "voice_rate",
    "voice_name", "bgm_type", "bgm_volume", "paragraph_number",
    "video_script_prompt",
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


def existing_names_ordered_from(cfg: dict[str, Any]) -> list[str]:
    """
    Return output_file values from an already-loaded config dict, in file
    order (oldest first), skipping entries with no output_file. Used so the
    LLM prompt can be told about topics that are already queued in the yaml
    but not yet rendered (not just ones already in seen.txt) — see
    prompt.build()'s seen_ordered param.
    """
    return [
        job.get("output_file", "")
        for job in (cfg.get("jobs") or [])
        if job.get("output_file")
    ]


def count_pending_from(cfg: dict[str, Any], seen: set[str]) -> int:
    """Count pending jobs from an already-loaded config dict."""
    return sum(
        1 for job in (cfg.get("jobs") or [])
        if job.get("enabled", True)
        and job.get("output_file", "") not in seen
    )


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
    Keys are written in _KEY_ORDER; anything else on the dict is dropped
    rather than passed through. Previously any extra key present (e.g.
    something the LLM hallucinated beyond the requested schema) was
    appended verbatim, which could confuse or break a downstream consumer
    with a stricter schema (MoneyPrinterTurbo) — this is a whitelist, not
    a blocklist, so it's safe by default against new unexpected keys too.
    """
    ordered: dict[str, Any] = {k: job[k] for k in _KEY_ORDER if k in job}

    dropped = sorted(set(job) - set(ordered))
    if dropped:
        print(f"  [note] dropping unexpected field(s) from job "
              f"{job.get('name', '?')!r}: {dropped}")

    lines: list[str] = []
    for i, (key, value) in enumerate(ordered.items()):
        prefix = "  - " if i == 0 else "    "
        lines.append(f"{prefix}{key}: {_scalar(value)}")
    return "\n".join(lines)


def _bootstrap_suffix(content: str) -> str | None:
    """
    Decide what EXTRA text (if any) must be appended after `content` and
    before the raw '  - key: val' job blocks, so those blocks parse as
    items of a top-level `jobs:` list.

    Strictly additive — this only ever returns text to be added at the
    end. It never edits, reorders, or removes a single byte already on
    disk, per this project's append-only invariant. Returns None when
    blind-appending is already safe as-is.

    Three bootstrap shapes are handled purely additively:
      - empty file                       → write "jobs:\n" (nothing to
                                            preserve, there's no content)
      - valid yaml, no "jobs:" key at all → append a new "jobs:\n" section
      - "jobs:" present with no value     → verified safe to append
        (bare key, i.e. YAML null)          after as-is, no edit needed

    One shape can NOT be made safe without editing an existing line:
      - "jobs: []" (an explicit empty list literal). Blindly appending
        after it produces invalid YAML (confirmed empirically), and the
        only fix is rewriting that one line — which append() must never
        do. So this raises with actionable instructions instead of
        silently corrupting the file or discarding data.
    """
    if not content.strip():
        return "jobs:\n"

    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(
            f"Refusing to append: jobs_<lang>.yaml is not valid YAML ({e}). "
            f"Fix the file manually first."
        ) from e

    if not isinstance(parsed, dict):
        # e.g. a bare top-level list — the exact shape a previous version
        # of this function could itself produce by appending after an
        # empty file. Never guess how to repair this.
        raise ValueError(
            "Refusing to append: jobs_<lang>.yaml's top level isn't a YAML "
            f"mapping with a 'jobs:' key (got {type(parsed).__name__}). "
            "The file was likely bootstrapped or corrupted incorrectly. "
            "It must start with a 'jobs:' key, e.g.:\n\njobs:\n"
        )

    if "jobs" not in parsed:
        # Pure addition — a new section after everything already there.
        header = "" if content.endswith("\n") else "\n"
        return header + "\njobs:\n"

    jobs_val = parsed.get("jobs")

    if jobs_val is None:
        # Bare "jobs:" key (YAML null) — appending an indented block
        # sequence right after it already parses correctly, no edit needed.
        return None

    if isinstance(jobs_val, list):
        if jobs_val:
            # Real entries already exist — append is safe exactly as before.
            return None
        raise ValueError(
            "Refusing to append: jobs_<lang>.yaml has 'jobs: []' (an "
            "explicit empty list) instead of a bare 'jobs:' key. Appending "
            "after it would produce invalid YAML, and fixing it would mean "
            "editing an existing line — which this tool never does "
            "automatically, so it never touches content you already have. "
            "Please edit the file by hand: change that line to just "
            "'jobs:' (remove the '[]'), then re-run."
        )

    raise ValueError(
        "Refusing to append: 'jobs:' in jobs_<lang>.yaml holds a value "
        f"that isn't a list ({type(jobs_val).__name__}). Fix the file "
        "manually first."
    )


def _atomic_write(path: Path, content: str) -> None:
    """
    Write `content` to `path` atomically: write to a temp file in the same
    directory, flush + fsync it, then os.replace() it over the target.

    os.replace() is a single atomic filesystem operation on POSIX and
    Windows — any concurrent reader (or a second writer that also lost the
    lock race) only ever observes the fully-old file or the fully-new one,
    never a half-written/interleaved mix. The temp file must be created in
    the same directory as `path` so the replace stays on one filesystem
    (a cross-filesystem rename is not atomic).

    Note this still can't merge two concurrent writers — it only
    guarantees the file is never left corrupted. See file_lock(strict=True)
    in lock.py for why append() additionally refuses to proceed without
    the lock rather than relying on this alone.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def append(jobs_dir: Path, lang: str, new_jobs: list[dict[str, Any]]) -> None:
    """
    Append new_jobs to the end of jobs_<lang>.yaml.

    The existing file content is never modified — new entries (and, if
    needed, a "jobs:" header for a file that doesn't have one yet — see
    _bootstrap_suffix) are added after the last line, preserving original
    style. The full new content is computed in memory and written with
    _atomic_write() (temp file + os.replace) rather than opened in append
    mode, so a reader can never observe a partially-written file, and two
    writers racing past the lock can't interleave their bytes into
    corrupted YAML.

    The lock itself is held in strict mode: unlike seen.py's single-line,
    idempotent appends (safe to fall back to unlocked on a timeout — worst
    case is a harmless duplicate line), this read-modify-replace cycle
    would otherwise risk a *lost update* on a lock timeout — one writer's
    replace silently wins and the other's whole batch of new jobs vanishes
    with no error. Refusing to proceed without the lock is safer than that.
    """
    p = _path(jobs_dir, lang)

    # Hold a lock across the read+replace so two concurrent refill runs
    # (e.g. two langs, or a retry racing a previous run) can't race each
    # other's read-modify-write cycle. strict=True: see docstring above.
    with file_lock(p, strict=True):
        content = p.read_text(encoding="utf-8")
        extra_header = _bootstrap_suffix(content)

        pieces = [content]
        if extra_header is not None:
            pieces.append(extra_header)
        elif content and not content.endswith("\n"):
            pieces.append("\n")
        for job in new_jobs:
            pieces.append("\n")
            pieces.append(_job_to_yaml(job))
            pieces.append("\n")

        _atomic_write(p, "".join(pieces))


# ── Utilities ─────────────────────────────────────────────────────────────────

def safe_name(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")[:60]
