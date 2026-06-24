"""
generator/seen.py

Tracks which output_file names have already been generated.
Storage: one plain-text file per language suffix, one filename per line.

File naming logic:
  file_suffix ""    → seen.txt         (default — no language separation)
  file_suffix "_es" → seen_es.txt
  file_suffix "_en" → seen_en.txt

Most users store everything in one seen.txt. Multi-language setups that
use file_suffix get separate files automatically.

To migrate to a database later, replace this module with one that
implements the same four functions: load / contains / add / add_many.
"""

from __future__ import annotations

from pathlib import Path

# In-memory cache keyed by resolved file path string.
# Stores entries in insertion order (list) for recency-aware slicing.
_cache: dict[str, list[str]] = {}


def _file(base_dir: Path, file_suffix: str) -> Path:
    """
    Resolve seen file path from file_suffix:
      ""     → seen.txt
      "_es"  → seen_es.txt
      "_en"  → seen_en.txt
    """
    if file_suffix:
        slug = file_suffix.lstrip("_")
        return base_dir / f"seen_{slug}.txt"
    return base_dir / "seen.txt"


def _key(base_dir: Path, file_suffix: str) -> str:
    return str(_file(base_dir, file_suffix))


def _load_list(base_dir: Path, file_suffix: str) -> list[str]:
    """Return entries in file order (oldest first). Uses cache."""
    k = _key(base_dir, file_suffix)
    if k not in _cache:
        f = _file(base_dir, file_suffix)
        if not f.exists():
            _cache[k] = []
        else:
            lines = f.read_text(encoding="utf-8").splitlines()
            seen_set: set[str] = set()
            ordered: list[str] = []
            for line in lines:
                entry = line.strip()
                if entry and entry not in seen_set:
                    seen_set.add(entry)
                    ordered.append(entry)
            _cache[k] = ordered
    return _cache[k]


def load(base_dir: Path, file_suffix: str) -> set[str]:
    """Return the full set of known output_file names for this suffix."""
    return set(_load_list(base_dir, file_suffix))


def load_ordered(base_dir: Path, file_suffix: str) -> list[str]:
    """Return entries in insertion order (oldest first). Used for recency-aware prompts."""
    return list(_load_list(base_dir, file_suffix))


def contains(base_dir: Path, file_suffix: str, output_file: str) -> bool:
    return output_file in load(base_dir, file_suffix)


def add(base_dir: Path, file_suffix: str, output_file: str) -> None:
    """Append output_file to the seen file. Idempotent."""
    existing = load(base_dir, file_suffix)
    if output_file in existing:
        return
    k = _key(base_dir, file_suffix)
    _cache[k].append(output_file)
    with open(_file(base_dir, file_suffix), "a", encoding="utf-8") as f:
        f.write(f"{output_file}\n")


def add_many(base_dir: Path, file_suffix: str, output_files: list[str]) -> None:
    """Append multiple entries at once (one file write)."""
    if not output_files:
        return
    existing = load(base_dir, file_suffix)
    new = [name for name in output_files if name not in existing]
    if not new:
        return
    k = _key(base_dir, file_suffix)
    _cache[k].extend(new)
    with open(_file(base_dir, file_suffix), "a", encoding="utf-8") as f:
        f.write("\n".join(new) + "\n")
