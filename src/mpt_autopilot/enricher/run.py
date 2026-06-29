#!/usr/bin/env python3
"""
enrich.py — hashtag-enricher entry point.

Usage examples:
  uv run enrich.py                              # scan current directory, auto-detect language
  uv run enrich.py --dir ./videos              # scan a specific folder
  uv run enrich.py --file ./videos/clip.mp4    # single file
  uv run enrich.py --dir ./videos --lang Spanish   # force language, skip LLM detection
  uv run enrich.py --dir ./videos --force      # re-generate even if hashtags already exist
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from hashtag_enricher.enricher.config import settings
from hashtag_enricher.enricher.logger import Logger
from hashtag_enricher.enricher.llm import detect_language, generate_hashtags
from hashtag_enricher.enricher.reader import resolve_meta
from hashtag_enricher.enricher.writer import build_hashtags_block, write_hashtags

log = Logger(settings.log_file, settings.max_log_size)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_file(
    mp4_path: Path,
    lang_override: str | None,
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
            with open(meta.json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if "hashtags" in existing:
                log.info(f"skip (already enriched): {mp4_path.name}")
                return "skipped"
        except (json.JSONDecodeError, OSError):
            log.warn(f"could not read {meta.json_path}, will re-generate")

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

        # Step 4: write
        write_hashtags(meta.json_path, block)

        log.info(f"ok: {mp4_path.name} → {len(tags)} tags ({meta.source})")
        return "ok"

    except Exception as exc:
        log.error(f"error processing {mp4_path.name}: {exc}")
        log.error(traceback.format_exc())
        return "error"


# ---------------------------------------------------------------------------
# Scanning helpers
# ---------------------------------------------------------------------------

def collect_mp4s(directory: Path) -> list[Path]:
    """Return all *.mp4 files in directory (non-recursive, sorted)."""
    files = sorted(directory.glob("*.mp4"))
    sub_count = len(list(directory.glob("**/*.mp4"))) - len(files)
    if sub_count > 0:
        log.info(
            f"[hint] {sub_count} mp4(s) found in subdirectories are not included. "
            "Move them to the top-level directory to process them."
        )
    return files


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enrich",
        description="Generate YouTube/TikTok hashtags for video files using an LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  uv run enrich.py                              scan current directory
  uv run enrich.py --dir ./videos              scan a folder
  uv run enrich.py --file clip.mp4             single file
  uv run enrich.py --dir ./videos --lang es    force Spanish
  uv run enrich.py --dir ./videos --force      re-generate existing hashtags
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
        "--force",
        action="store_true",
        help="Re-generate hashtags even if they already exist in the json file",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    force: bool = args.force
    lang_override: str | None = args.lang

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
        log.info("No *.mp4 files found.")
        sys.exit(0)

    log.info(f"=== hashtag-enricher: {len(mp4_files)} file(s) to process ===")

    # Process
    counts: dict[str, int] = {"ok": 0, "skipped": 0, "error": 0}

    for mp4_path in mp4_files:
        result = process_file(mp4_path, lang_override, force)
        counts[result] += 1

    # Summary
    log.info("=" * 50)
    log.info(f"Done. ok={counts['ok']}  skipped={counts['skipped']}  error={counts['error']}")
    log.info("=" * 50)

    if counts["error"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
