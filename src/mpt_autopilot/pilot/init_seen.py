#!/usr/bin/env python3
"""
init_seen.py — register existing .mp4 files into seen.txt.

Scans directories for .mp4 files and adds their names to the seen file
so refill won't generate duplicate ideas for videos that already exist.

Safe to run multiple times — already-known names are never duplicated.

Without --lang (recommended for most users):
    Scans all files and registers them into seen.txt. No filtering.
    Use this if you work with one language or don't separate videos by lang.

With --lang:
    Filters files by the lang's file_suffix from config.yaml and writes
    to the corresponding seen file (seen.txt, seen_es.txt, etc.).
    Use this for multi-language setups where each lang has its own seen file.

Usage:
    init-seen --dir /your/path/to/videos
    init-seen --dir /videos --dir /videos/old
    init-seen --lang es --dir /your/path/to/videos
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from shorts_pilot.generator import seen
from shorts_pilot.generator.settings import load as load_settings

# Matches language-code suffixes like _es, _en, _de, _fr, _pt, _zh (2-3 chars).
_LANG_SUFFIX_RE = re.compile(r"_[a-z]{2,3}\.mp4$", re.IGNORECASE)


def collect_mp4_names(*dirs: Path) -> set[str]:
    names: set[str] = set()
    for d in dirs:
        if not d.exists():
            print(f"  [skip] not found: {d}")
            continue
        found = {f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".mp4"}
        print(f"  {d}: {len(found)} .mp4 files")
        names.update(found)
    return names


def _filter_by_suffix(names: set[str], file_suffix: str, all_suffixes: set[str]) -> set[str]:
    """
    Keep only filenames that belong to this lang's file_suffix.

    file_suffix "_es" → keep files ending with "_es.mp4"
    file_suffix ""    → keep files that do NOT end with any known suffix from config

    Uses the full set of known suffixes from config.yaml so detection is always
    exact — no regex guessing needed.
    """
    if file_suffix:
        return {n for n in names if n.lower().endswith(f"{file_suffix}.mp4")}
    else:
        return {
            n for n in names
            if not any(n.lower().endswith(f"{s}.mp4") for s in all_suffixes)
        }


def init_all(scan_dirs: list[Path], seen_dir: Path) -> int:
    """
    No --lang mode: register ALL .mp4 files into seen.txt without any filtering.
    Simple and unambiguous for single-language users.
    """
    print("→ seen.txt (no lang filter — registering all files)")
    found = collect_mp4_names(*scan_dirs)
    existing = seen.load(seen_dir, "")
    new_entries = found - existing

    print(f"  found on disk      : {len(found)}")
    print(f"  already registered : {len(existing)}")
    print(f"  new to add         : {len(new_entries)}")

    if new_entries:
        seen.add_many(seen_dir, "", sorted(new_entries))
        print(f"  [OK] added {len(new_entries)} entries to seen.txt")
    else:
        print("  [OK] nothing new to add")

    return len(new_entries)


def init_lang(
        lang: str,
        file_suffix: str,
        all_suffixes: set[str],
        scan_dirs: list[Path],
        seen_dir: Path,
) -> int:
    """--lang mode: filter by suffix and write to the matching seen file."""
    seen_filename = "seen.txt" if not file_suffix else f"seen_{file_suffix.lstrip('_')}.txt"
    print(f"[{lang}] → {seen_filename}")

    found = collect_mp4_names(*scan_dirs)
    matched = _filter_by_suffix(found, file_suffix, all_suffixes)
    skipped = len(found) - len(matched)
    existing = seen.load(seen_dir, file_suffix)
    new_entries = matched - existing

    print(f"  found on disk      : {len(found)}")
    if skipped:
        print(f"  filtered out       : {skipped} (belong to a different lang)")
    print(f"  matched this lang  : {len(matched)}")
    print(f"  already registered : {len(existing)}")
    print(f"  new to add         : {len(new_entries)}")

    if new_entries:
        seen.add_many(seen_dir, file_suffix, sorted(new_entries))
        print(f"  [OK] added {len(new_entries)} entries to {seen_filename}")
    else:
        print("  [OK] nothing new to add")

    return len(new_entries)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="init-seen",
        description=(
            "Scan directories for existing .mp4 files and register their names "
            "so refill won't generate duplicates. "
            "Safe to run multiple times."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Most users: register all videos into seen.txt (no lang filter)
    init-seen --dir /your/path/to/videos

    # Multiple directories
    init-seen --dir /your/path/to/videos --dir /your/path/to/videos/old

    # Multi-language: filter by lang and write to separate seen files
    init-seen --lang en --dir /your/path/to/videos
    init-seen --lang es --dir /your/path/to/videos
""",
    )
    parser.add_argument(
        "--lang",
        default=None,
        metavar="LANG",
        help=(
            "Language to process (e.g. en, es). "
            "If omitted, all files are registered into seen.txt without filtering."
        ),
    )
    parser.add_argument(
        "--dir",
        metavar="PATH",
        type=Path,
        action="append",
        dest="dirs",
        default=[],
        help=(
            "Directory to scan for .mp4 files. "
            "Can be passed multiple times. "
            "Combined with scan_dirs from config.yaml."
        ),
    )
    parser.add_argument(
        "--seen-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Directory where seen*.txt files are stored. "
            "Default: paths.seen_dir from config.yaml, else current directory."
        ),
    )

    args = parser.parse_args()

    try:
        settings = load_settings()
    except EnvironmentError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    seen_dir = (args.seen_dir or settings.seen_dir or Path(".")).resolve()
    config_dirs = [Path(d) for d in (settings.scan_dirs or [])]
    cli_dirs = [d.resolve() for d in args.dirs]
    all_dirs = config_dirs + cli_dirs

    if not all_dirs:
        print("[ERROR] No directories to scan.")
        print("  Pass --dir /path/to/videos or add paths to scan_dirs in config.yaml.")
        sys.exit(1)

    if args.lang is None:
        # Simple mode: no filtering, everything into seen.txt
        added = init_all(all_dirs, seen_dir)
    else:
        # Lang mode: filter by suffix, write to the lang's seen file
        try:
            lang_cfg = settings.lang(args.lang)
        except ValueError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)

        all_suffixes = {cfg.file_suffix for cfg in settings.langs.values() if cfg.file_suffix}
        added = init_lang(args.lang, lang_cfg.file_suffix, all_suffixes, all_dirs, seen_dir)

    print(f"\n[done] added {added} new entries.")


if __name__ == "__main__":
    main()