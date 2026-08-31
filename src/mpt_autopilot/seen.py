"""
seen.py — the single dedup registry shared by every MPT Autopilot stage.

Tracks which `output_file` names have already been produced. Storage is a plain
text file, one filename per line, append-only: a crash mid-run never loses
progress, and re-running picks up exactly where it left off.

This module merges two previously separate implementations (the batch stage's
path-first registry and the pilot stage's suffix-aware one). The public API is
path-first; multi-language suffix handling lives in `resolve()`:

    resolve(dir, "")    → <dir>/seen.txt
    resolve(dir, "_es") → <dir>/seen_es.txt

Entries are cached per resolved path and kept in insertion order, because the
pilot stage feeds the most recent entries back into its prompts.

To migrate to a database later, replace this module with one implementing the
same functions: resolve / load / load_ordered / contains / add / add_many /
list_all.
"""

from __future__ import annotations

from pathlib import Path

from mpt_autopilot.lock import file_lock

# Cache keyed by the resolved path string, holding entries in file order
# (oldest first) with duplicates already removed.
_cache: dict[str, list[str]] = {}


def resolve(base_dir: Path, file_suffix: str = "") -> Path:
    """
    Build the seen-file path for a language suffix.

      ""     → seen.txt
      "_es"  → seen_es.txt
      "es"   → seen_es.txt   (a missing leading underscore is tolerated)
    """
    if file_suffix:
        slug = file_suffix.lstrip("_")
        return base_dir / f"seen_{slug}.txt"
    return base_dir / "seen.txt"


def _load_list(path: Path) -> list[str]:
    key = str(path)
    if key not in _cache:
        if not path.exists():
            _cache[key] = []
        else:
            ordered: list[str] = []
            known: set[str] = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = line.strip()
                if entry and entry not in known:
                    known.add(entry)
                    ordered.append(entry)
            _cache[key] = ordered
    return _cache[key]


def load(path: Path) -> set[str]:
    """Every known output_file name. Cached per path."""
    return set(_load_list(path))


def load_ordered(path: Path) -> list[str]:
    """Entries in insertion order (oldest first) — used for recency-aware prompts."""
    return list(_load_list(path))


def contains(path: Path, output_file: str) -> bool:
    return output_file in load(path)


def add(path: Path, output_file: str) -> None:
    """Append one entry. Idempotent."""
    if output_file in load(path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock around the write so two concurrent processes can't interleave bytes
    # mid-write. A duplicate line from a rare race is harmless — _load_list()
    # already de-duplicates on read.
    with file_lock(path):
        _cache[str(path)].append(output_file)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{output_file}\n")


def add_many(path: Path, output_files: list[str]) -> None:
    """Append several entries in one write. Idempotent."""
    if not output_files:
        return
    existing = load(path)
    new = [name for name in output_files if name not in existing]
    if not new:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        _cache[str(path)].extend(new)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(new) + "\n")


def list_all(path: Path) -> list[str]:
    """All registered names, sorted alphabetically."""
    return sorted(load(path))
