"""
engine/state.py — In-progress job tracking.

Persists (output_file, task_id, attempt) right after submit_job succeeds,
before polling begins. On restart after a crash or Ctrl-C, pending entries
are resumed (polled to completion) instead of creating duplicate MPT tasks.
"""

from __future__ import annotations

import json
from pathlib import Path


def add(path: Path, output_file: str, task_id: str, attempt: int) -> None:
    """Record an in-progress job."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps({"output_file": output_file, "task_id": task_id, "attempt": attempt}) + "\n"
        )


def remove(path: Path, output_file: str) -> None:
    """Remove all entries for output_file (rewrites file)."""
    entries = _read_all(path)
    filtered = [e for e in entries if e.get("output_file") != output_file]
    if len(filtered) != len(entries):
        _write_all(path, filtered)


def list_all(path: Path) -> list[dict]:
    """All in-progress entries, each with output_file, task_id, attempt."""
    return _read_all(path)


def _read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _write_all(path: Path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
