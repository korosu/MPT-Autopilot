"""
batch/state.py — In-progress job tracking.

Persists (output_file, task_id, attempt) right after submit_job succeeds,
before polling begins. On restart after a crash or Ctrl-C, pending entries
are resumed (polled to completion) instead of creating duplicate MPT tasks.

The file is one JSON object per line. Every rewrite goes through a temp file
plus os.replace(), so a crash mid-write leaves either the old complete file or
the new complete file — never a truncated one. A truncated file would make a
previous run's task_id unrecoverable, and the next run would submit a duplicate
MPT task for the same video.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from mpt_autopilot.lock import file_lock

_logger = logging.getLogger(__name__)


def add(path: Path, output_file: str, task_id: str, attempt: int) -> None:
    """
    Record an in-progress job, durably and under the lock.

    This is the write the whole module exists for: the task_id is already
    submitted to MPT, so losing it to a crash means the next run submits a
    duplicate. fsync (like _write_all) makes the line survive power loss,
    and file_lock keeps a concurrent process's append from interleaving.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps({"output_file": output_file, "task_id": task_id, "attempt": attempt})
                + "\n"
            )
            f.flush()
            os.fsync(f.fileno())


def remove(path: Path, output_file: str) -> None:
    """Remove all entries for output_file (rewrites the file atomically)."""
    entries = _read_all(path)
    filtered = [e for e in entries if e.get("output_file") != output_file]
    if len(filtered) != len(entries):
        _write_all(path, filtered)


def list_all(path: Path) -> list[dict]:
    """All in-progress entries, each with output_file, task_id, attempt."""
    return _read_all(path)


def _read_all(path: Path) -> list[dict]:
    """
    Parse every line. A corrupt line raises rather than being dropped.

    Silently skipping a corrupt line used to look harmless — but that line
    held the task_id of a job that was already submitted to MPT. Dropping it
    meant the resume pass skipped it, and the next run submitted a second MPT
    task for the same video. Failing loudly is cheaper than a duplicate job.

    Duplicate output_file entries (from a partial write before a crash) are
    de-duplicated keeping the last occurrence, since it reflects the most
    recent attempt.
    """
    if not path.exists():
        return []
    entries: list[dict] = []
    seen_keys: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path} line {lineno} is not valid JSON ({exc.msg}); the file is "
                f"corrupt. Fix or delete it, then re-run — dropping this line would "
                f"lose the task_id of an already-submitted job and cause a duplicate "
                f"MPT task."
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"{path} line {lineno} is {type(parsed).__name__}, expected a JSON object"
            )
        key = parsed.get("output_file")
        if key is not None:
            if key in seen_keys:
                # Replace previous duplicate: pop it and log.
                entries = [e for e in entries if e.get("output_file") != key]
                _logger.warning(
                    "duplicate in_progress entry for output_file=%r (line %d) — keeping latest",
                    key,
                    lineno,
                )
            seen_keys.add(key)
        entries.append(parsed)
    return entries


def _write_all(path: Path, entries: list[dict]) -> None:
    """
    Rewrite the file atomically: temp file in the same dir, then os.replace().

    mkstemp (as in seen.py/jobs.py) instead of a fixed ``.tmp`` name: two
    concurrent writers would otherwise clobber each other's temp file and
    one os.replace() would publish the other's half-written content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
