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

import logging
import os
import tempfile
from pathlib import Path

from mpt_autopilot.lock import file_lock

# Cache keyed by the resolved path string, holding the file fingerprint the
# entries were read from plus entries in file order (oldest first), with
# duplicates already removed.
_cache: dict[str, tuple[float, int, list[str]]] = {}

_logger = logging.getLogger(__name__)
_rotation_max_bytes: int = 64 * 1024 * 1024  # 64 MB default; set via set_rotation_max_bytes()


def set_rotation_max_bytes(n: int) -> None:
    """Override the file-size threshold that triggers rotation (bytes)."""
    global _rotation_max_bytes
    _rotation_max_bytes = max(n, 1024)


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


def _key(path: Path) -> str:
    """
    Canonical cache key.

    Keying on str(path) meant "./seen.txt" and the absolute path to the same
    file cached separately — one entry could be stale while the other was
    fresh, so contains() could miss an already-registered name.
    """
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _fingerprint(path: Path) -> tuple[float, int]:
    try:
        s = path.stat()
        return (s.st_mtime, s.st_size)
    except OSError:
        return (0.0, 0)


def _read_entries(path: Path) -> list[str]:
    ordered: list[str] = []
    known: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ordered
    for line in text.splitlines():
        entry = line.strip()
        if entry and entry not in known:
            known.add(entry)
            ordered.append(entry)
    return ordered


def _load_list(path: Path) -> list[str]:
    """
    Entries cached per resolved path, invalidated when the file's mtime or size
    changes. Without the fingerprint an append by another process would be
    cached forever; without the resolved key two spellings of the same file
    would hold two inconsistent caches.
    """
    key = _key(path)
    fp = _fingerprint(path)
    cached = _cache.get(key)
    if cached is not None and (cached[0], cached[1]) == fp:
        return cached[2]
    entries = _read_entries(path)
    _cache[key] = (fp[0], fp[1], entries)
    return entries


def clear_cache(path: Path | None = None) -> None:
    """Drop one path's cache entry, or every entry when path is None."""
    if path is None:
        _cache.clear()
    else:
        _cache.pop(_key(path), None)


def load(path: Path) -> set[str]:
    """Every known output_file name. Cached per path."""
    return set(_load_list(path))


def load_ordered(path: Path) -> list[str]:
    """Entries in insertion order (oldest first) — used for recency-aware prompts."""
    return list(_load_list(path))


def contains(path: Path, output_file: str) -> bool:
    return output_file in load(path)


def _contains_cached(path: Path, output_file: str) -> bool:
    """
    Return True if output_file is in the in-memory cache for path.

    Returns False when the cache is empty or stale, so the caller falls back
    to a full load() — this keeps add() and add_many() idempotent even when
    another process has written to the file.
    """
    key = _key(path)
    cached = _cache.get(key)
    if cached is None:
        return False
    _fp, _size, entries = cached
    return output_file in entries


# ── Rotation ───────────────────────────────────────────────────────────────────


def _do_rotate(entries: list[str]) -> list[str] | None:
    """
    Compute which entries to keep to fit within the rotation limit.
    Returns kept entries, or None if no rotation needed.
    Pure computation — no file I/O.
    """
    if len(entries) <= 1:
        return None
    total_size = sum(len(e) + 1 for e in entries)
    if total_size <= _rotation_max_bytes:
        return None
    # Trim oldest entries until the remaining ones fit
    for n in range(len(entries), 0, -1):
        candidate = entries[-n:]
        approx_size = sum(len(e) + 1 for e in candidate)
        if approx_size <= _rotation_max_bytes:
            return candidate
    return None


def _perform_rotation(path: Path, entries: list[str]) -> None:
    """
    Atomically write rotated entries to disk. Caller must hold file_lock(path).

    Used by add()/add_many() which already hold the lock — avoids
    re-entrant deadlock that would occur if they called rotate() directly.
    """
    kept = _do_rotate(entries)
    if kept is None or len(kept) == len(entries):
        return
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rotate(path: Path) -> None:
    """
    If the file exceeds `_rotation_max_bytes`, trim the oldest entries until it
    fits. The lock is held for the entire read-trim-write so a concurrent
    writer never observes a half-written file, and any entries appended
    between this call and the read are included in the decision.
    The in-memory cache is updated to match the new on-disk content.

    A no-op when the file is under the limit, has no entries, or does not exist.
    """
    if not path.exists() or path.stat().st_size <= _rotation_max_bytes:
        return
    with file_lock(path):
        entries = _read_entries(path)
        if len(entries) <= 1:
            return
        kept: list[str] | None = None
        for n in range(1, len(entries) + 1):
            candidate = entries[-n:]
            approx_size = sum(len(e) + 1 for e in candidate)
            if approx_size <= _rotation_max_bytes:
                kept = candidate
                break
        if kept is None or len(kept) == len(entries):
            return
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(kept) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    key = _key(path)
    _cache[key] = (*_fingerprint(path), kept)
    _logger.info(
        "seen rotation: trimmed %s to %d entries (%d bytes, limit %d MB)",
        path.name,
        len(kept),
        path.stat().st_size,
        _rotation_max_bytes // (1024 * 1024),
    )


def _write_entries(path: Path, entries: list[str]) -> None:
    """Atomically write entries to path. Caller must hold file_lock(path)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(entries) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Writing ────────────────────────────────────────────────────────────────────


def add(path: Path, output_file: str) -> None:
    """Append one entry. Idempotent."""
    if _contains_cached(path, output_file):
        return
    if output_file in load(path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock around the write so two concurrent processes can't interleave bytes
    # mid-write. A duplicate line from a rare race is harmless — _read_entries()
    # already de-duplicates on read.
    with file_lock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{output_file}\n")
        key = _key(path)
        prev = _cache.get(key)
        entries = list(prev[2]) if prev is not None else []
        if output_file not in entries:
            entries.append(output_file)
        _cache[key] = (*_fingerprint(path), entries)
        rotate(path)


def add_many(path: Path, output_files: list[str]) -> None:
    """Append several entries in one write. Idempotent."""
    if not output_files:
        return
    # Fast path: filter out entries already in the in-memory cache.
    cached_new = [n for n in output_files if not _contains_cached(path, n)]
    if not cached_new:
        return
    # Cold cache or some entries not cached: full load for the remainder.
    existing = load(path)
    new = [n for n in cached_new if n not in existing]
    if not new:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(new) + "\n")
        key = _key(path)
        prev = _cache.get(key)
        entries = list(prev[2]) if prev is not None else []
        for name in new:
            if name not in entries:
                entries.append(name)
        _cache[key] = (*_fingerprint(path), entries)
        _perform_rotation(path, entries)


def list_all(path: Path) -> list[str]:
    """All registered names, sorted alphabetically."""
    return sorted(load(path))
