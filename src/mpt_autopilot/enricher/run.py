#!/usr/bin/env python3
"""
enrich.py — hashtag-enricher entry point.

Usage examples:
  python enrich.py                              # scan current directory, auto-detect language
  python enrich.py --dir ./videos              # scan a specific folder
  python enrich.py --file ./videos/clip.mp4    # single file
  python enrich.py --dir ./videos --lang Spanish   # force language, skip LLM detection
  python enrich.py --dir ./videos --dry-run    # preview without writing
  python enrich.py --dir ./videos --force      # re-generate even if hashtags already exist
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from enricher.config import settings
from enricher.logger import Logger
from enricher.llm import detect_language, generate_hashtags
from enricher.reader import resolve_meta
from enricher.writer import build_hashtags_block, write_hashtags

log = Logger(settings.log_file, settings.max_log_size)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_file(
    mp4_path: Path,
    lang_override: str | None,
    dry_run: bool,
    force: bool,
) -> str:
    """
    Process a single mp4 file.

    Returns one of: "ok" | "skipped" | "error"
    """
    meta = resolve_meta(mp4_path, lang_override=lang_override)

    # Skip if hashtags already exist and --force not set
    if not force and meta.json_path.exists():
        try:
            import json
            with open(meta.json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if "hashtags" in existing:
                log.info(f"skip (already enriched): {mp4_path.name}")
                return "skipped"
        except Exception:
            pass  # If we can't read it, proceed and try to write

    try:
        # Step 1: resolve language
        if meta.language_hint:
            language = meta.language_hint
            log.info(f"lang='{language}' (provided) | topic='{meta.topic}' | {mp4_path.name}")
        else:
            log.info(f"detecting language for: {mp4_path.name}")
            language = detect_language(meta.topic)
            log.info(f"lang='{language}' (detected) | topic='{meta.topic}'")

        # Step 2: generate hashtags
        tags = generate_hashtags(meta.topic, language)

        if not tags:
            # Fallback: minimal safe output so the file is still useful
            log.warn(f"LLM returned empty tags for {mp4_path.name}, using fallback")
            tags = ["#shorts"]

        # Step 3: build result block
        block = build_hashtags_block(
            tags_list=tags,
            language=language,
            model=settings.model,
            source=meta.source,
        )

        # Step 4: write (or dry-run preview)
        write_hashtags(meta.json_path, block, dry_run=dry_run)

        if not dry_run:
            log.info(f"ok: {mp4_path.name} → {len(tags)} tags ({meta.source})")
        return "ok"

    except Exception as exc:
        log.error(f"error processing {mp4_path.name}: {exc}")
        return "error"


# ---------------------------------------------------------------------------
# Scanning helpers
# ---------------------------------------------------------------------------

def collect_mp4s(directory: Path) -> list[Path]:
    """Return all *.mp4 files in directory (non-recursive, sorted)."""
    return sorted(directory.glob("*.mp4"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enrich.py",
        description="Generate YouTube hashtags for video files using an LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python enrich.py                              scan current directory
  python enrich.py --dir ./videos              scan a folder
  python enrich.py --file clip.mp4             single file
  python enrich.py --dir ./videos --lang es    force Spanish
  python enrich.py --dir ./videos --dry-run    preview only
  python enrich.py --dir ./videos --force      re-generate existing hashtags
        """,
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--dir",
        metavar="PATH",
        type=Path,
        help="Directory to scan for *.mp4 files (default: current directory)",
    )
    source.add_argument(
        "--file",
        metavar="FILE",
        type=Path,
        help="Process a single mp4 file",
    )

    parser.add_argument(
        "--lang",
        metavar="LANGUAGE",
        default=None,
        help=(
            "Force a specific language for all files, e.g. 'English', 'Spanish', 'ru'. "
            "If omitted, language is detected automatically per file."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be written without saving anything",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-generate hashtags even if they already exist in the json file",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dry_run: bool = args.dry_run
    force: bool = args.force
    lang_override: str | None = args.lang

    if dry_run:
        print("[dry-run mode — nothing will be written]")

    # Collect files to process
    if args.file:
        target = args.file.resolve()
        if not target.exists():
            log.error(f"File not found: {target}")
            sys.exit(1)
        if target.suffix.lower() != ".mp4":
            log.error(f"Not an mp4 file: {target}")
            sys.exit(1)
        mp4_files = [target]
    else:
        directory = (args.dir or Path(".")).resolve()
        if not directory.is_dir():
            log.error(f"Directory not found: {directory}")
            sys.exit(1)
        mp4_files = collect_mp4s(directory)

    if not mp4_files:
        print("No *.mp4 files found.")
        sys.exit(0)

    log.info(f"=== hashtag-enricher: {len(mp4_files)} file(s) to process ===")

    # Process
    counts: dict[str, int] = {"ok": 0, "skipped": 0, "error": 0}

    for mp4_path in mp4_files:
        result = process_file(mp4_path, lang_override, dry_run, force)
        counts[result] += 1

    # Summary
    log.info("=" * 50)
    log.info(f"Done. ok={counts['ok']}  skipped={counts['skipped']}  error={counts['error']}")
    log.info("=" * 50)

    if counts["error"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
