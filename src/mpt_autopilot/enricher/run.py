#!/usr/bin/env python3
"""
enrich.py — hashtag-enricher entry point.

Usage examples:
  uv run enrich                                      # scan current directory
  uv run enrich --dir ./videos                       # scan a specific folder
  uv run enrich --file ./videos/clip.mp4             # single file
  uv run enrich --dir ./videos --lang Spanish        # force language (skips detection)
  uv run enrich --dir ./videos --platform tiktok     # target TikTok
  uv run enrich --dir ./videos --force               # re-generate existing
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from hashtag_enricher.enricher.config import settings
from hashtag_enricher.enricher.logger import Logger
from hashtag_enricher.enricher.llm import detect_and_generate, generate_hashtags
from hashtag_enricher.enricher.reader import resolve_meta
from hashtag_enricher.enricher.writer import build_hashtags_block, write_hashtags

# Initialise logger lazily (avoids triggering settings at import time)
_log: Logger | None = None


def _get_log() -> Logger:
    global _log
    if _log is None:
        _log = Logger(settings.log_file, settings.max_log_size)
    return _log


# ── Core processing ───────────────────────────────────────────────────────────

def process_file(
    mp4_path: Path,
    lang_override: str | None,
    force: bool,
    platform_override: str | None = None,
) -> str:
    """
    Process a single mp4 file.

    Returns one of: "ok" | "skipped" | "error"
    """
    log = _get_log()
    meta = resolve_meta(mp4_path, lang_override=lang_override)

    # ── Skip if already enriched (unless --force) ─────────────────────────────
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
        # ── Resolve platform ──────────────────────────────────────────────────
        platform = platform_override or settings.platform

        # ── Generate hashtags ─────────────────────────────────────────────────
        # If a language is already known (--lang flag, or video_language in an
        # existing script.json), generate_hashtags() is called directly — this
        # is a SINGLE API call and never triggers language detection.
        # Only when no language is known does detect_and_generate() run, which
        # detects the language AND generates tags in one combined API call.
        if meta.language_hint:
            language = meta.language_hint
            tags = generate_hashtags(meta.topic, language)
        else:
            language, tags = detect_and_generate(meta.topic)

        if not tags:
            log.warn(f"LLM returned empty tags for {mp4_path.name}, using fallback")
            tags = list(settings.always_include) or ["#shorts"]

        # ── Build and write output ────────────────────────────────────────────
        block = build_hashtags_block(
            tags_list=tags,
            language=language,
            model=settings.model,
            source=meta.source,
            platform=platform,
        )
        write_hashtags(meta.json_path, block)

        tags_str = " ".join(tags)
        lang_origin = "provided" if meta.language_hint else "detected"
        log.info(
            f"ok: {mp4_path.name} → {tags_str} "
            f"({len(tags)} tags, lang={language} [{lang_origin}], "
            f"platform={platform}, source={meta.source})"
        )
        return "ok"

    except Exception as exc:
        log.error(f"error processing {mp4_path.name}: {exc}")
        log.error(traceback.format_exc())
        return "error"


# ── Scanning helpers ──────────────────────────────────────────────────────────

def collect_mp4s(directory: Path) -> list[Path]:
    """Return all *.mp4 files in directory (non-recursive, sorted)."""
    log = _get_log()
    files = sorted(directory.glob("*.mp4"))
    sub_count = len(list(directory.glob("**/*.mp4"))) - len(files)
    if sub_count > 0:
        log.info(
            f"[hint] {sub_count} mp4(s) found in subdirectories are not included. "
            "Move them to the top-level directory to process them."
        )
    return files


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enrich",
        description="Generate YouTube/TikTok/Instagram hashtags for video files using an LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  uv run enrich                              scan current directory
  uv run enrich --dir ./videos              scan a folder
  uv run enrich --file clip.mp4             single file
  uv run enrich --dir ./videos --lang es    force Spanish (skips detection)
  uv run enrich --dir ./videos --platform tiktok   target TikTok (3–5 tags)
  uv run enrich --dir ./videos --force      re-generate existing hashtags
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
            "Skips LLM language detection entirely (single API call instead of two). "
            "If omitted, language is detected automatically per file."
        ),
    )
    parser.add_argument(
        "--platform",
        metavar="PLATFORM",
        choices=["youtube", "tiktok", "instagram"],
        default=None,
        help=(
            "Target platform: youtube (default), tiktok, instagram. "
            "Overrides the 'platform' setting in config.yaml. "
            "Affects tag count limits (all platforms: 3–5 optimal)."
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

    log = _get_log()

    force: bool = args.force
    lang_override: str | None = args.lang
    platform_override: str | None = args.platform

    # ── Collect files ─────────────────────────────────────────────────────────
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

    effective_platform = platform_override or settings.platform
    log.info(
        f"=== hashtag-enricher: {len(mp4_files)} file(s) | "
        f"platform={effective_platform} | "
        f"tags={settings.min_tags}–{settings.max_tags} ==="
    )

    # ── Process ───────────────────────────────────────────────────────────────
    counts: dict[str, int] = {"ok": 0, "skipped": 0, "error": 0}

    for mp4_path in mp4_files:
        result = process_file(
            mp4_path,
            lang_override=lang_override,
            force=force,
            platform_override=platform_override,
        )
        counts[result] += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("=" * 55)
    log.info(
        f"Done. ok={counts['ok']}  skipped={counts['skipped']}  error={counts['error']}"
    )
    log.info("=" * 55)

    if counts["error"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
