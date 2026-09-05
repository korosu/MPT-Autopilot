#!/usr/bin/env python3
"""
enricher/run.py — enricher stage entry point (`mpt enrich`).

Usage examples:
  mpt enrich                                    # scan current directory
  mpt enrich --dir ./videos                     # scan a specific folder
  mpt enrich --file ./videos/clip.mp4           # single file
  mpt enrich --dir ./videos --lang Spanish      # force language (skips detection)
  mpt enrich --dir ./videos --platform tiktok   # target TikTok
  mpt enrich --dir ./videos --force             # re-generate existing
  mpt enrich --dir ./videos --dry-run           # list candidates only
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from mpt_autopilot.config import ConfigError
from mpt_autopilot.enricher.llm import detect_and_generate, generate_hashtags
from mpt_autopilot.enricher.reader import resolve_meta
from mpt_autopilot.enricher.settings import configure, settings, validate_tag_budget
from mpt_autopilot.enricher.writer import build_hashtags_block, write_hashtags
from mpt_autopilot.logger import Logger
from mpt_autopilot.notify import alert

# Initialise logger lazily (avoids triggering settings at import time)
_log: Logger | None = None


def _get_log() -> Logger:
    global _log
    if _log is None:
        _log = Logger(settings.log_file, settings.max_log_size, name="mpt_autopilot.enricher")
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
            with open(meta.json_path, encoding="utf-8") as f:
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
        # `platform` is passed explicitly to both so the {platform} prompt
        # placeholder and the tag hard-limit follow --platform, not just
        # config.yaml's platform setting (main() has already validated that
        # max_tags + always_include fits this platform before we get here).
        if meta.language_hint:
            language = meta.language_hint
            tags = generate_hashtags(meta.topic, language, platform=platform)
        else:
            language, tags = detect_and_generate(meta.topic, platform=platform)

        if not tags:
            log.warn(f"LLM returned empty tags for {mp4_path.name}, using fallback")
            tags = list(settings.always_include)
            if not tags:
                log.warn("No always_include configured — writing empty tags")

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
        alert(f"Error: {mp4_path.name} - {exc}", settings)
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


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register the enricher stage's flags on `parser`.

    Kept separate from build_parser() so `mpt`'s subparser and a standalone
    parser share one definition — the CLI owns `--config` at the root level, so
    it is deliberately not registered here.
    """
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--dir",
        metavar="PATH",
        type=Path,
        help=(
            "Directory to scan for *.mp4 files "
            "(default: enricher.videos_dir from config.yaml, else the current directory)"
        ),
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
            "Overrides the 'platform' setting in config.yaml for this run — "
            "affects both the prompt and the tag count hard limit."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-generate hashtags even if they already exist in the json file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "List the files that would be enriched and exit without calling the "
            "LLM or writing anything."
        ),
    )

    return parser


EPILOG = """\
Examples:
  mpt enrich                              scan current directory
  mpt enrich --dir ./videos               scan a folder
  mpt enrich --file clip.mp4              single file
  mpt enrich --dir ./videos --lang es     force Spanish (skips detection)
  mpt enrich --dir ./videos --platform tiktok   target TikTok (3–5 tags)
  mpt enrich --dir ./videos --force       re-generate existing hashtags
  mpt enrich --dir ./videos --dry-run     list candidates, call nothing
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt enrich",
        description="Generate YouTube/TikTok/Instagram hashtags for video files using an LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Config file path (default: config.yaml)",
    )
    return add_arguments(parser)


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    """
    Run the enricher stage from already-parsed arguments.

    Returns an exit code instead of calling sys.exit so `mpt run` can aggregate
    stages in one process.
    """
    if config_path is None:
        config_path = getattr(args, "config", None)
    # Must happen before anything touches the lazy `settings` singleton.
    configure(config_path)

    dry_run: bool = getattr(args, "dry_run", False)
    force: bool = args.force
    lang_override: str | None = args.lang
    platform_override: str | None = args.platform

    try:
        log = _get_log()
    except (ConfigError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    # ── Collect files ─────────────────────────────────────────────────────────
    if args.file:
        target = args.file.resolve()
        if not target.exists():
            log.error(f"File not found: {target}")
            return 1
        if target.suffix.lower() != ".mp4":
            log.error(f"Not an mp4 file: {target}")
            return 1
        mp4_files = [target]
    else:
        directory = (args.dir or settings.directory or Path(".")).resolve()
        if not directory.is_dir():
            log.error(f"Directory not found: {directory}")
            return 1
        mp4_files = collect_mp4s(directory)

    if not mp4_files:
        log.info("No *.mp4 files found.")
        return 0

    effective_platform = platform_override or settings.platform

    # enricher/settings.py already validated this for settings.platform at
    # startup, but --platform can point at a *different*, possibly stricter,
    # platform (e.g. config.yaml says youtube, --platform tiktok caps at 5).
    # Re-check here so a bad combination fails fast for the whole run instead of
    # silently over-generating and getting truncated per-file later.
    if platform_override:
        try:
            validate_tag_budget(platform_override, settings.max_tags, len(settings.always_include))
        except ValueError as exc:
            log.error(f"--platform {platform_override}: {exc}")
            return 1

    if dry_run:
        log.info(
            f"=== enrich --dry-run: {len(mp4_files)} file(s) | "
            f"platform={effective_platform} | "
            f"tags={settings.min_tags}–{settings.max_tags} ==="
        )
        for mp4_path in mp4_files:
            meta = resolve_meta(mp4_path, lang_override=lang_override)
            state = "would re-generate" if force else _dry_run_state(meta.json_path)
            log.info(f"  {mp4_path.name}: {state}")
        log.info("Dry run — no LLM calls, no files written.")
        return 0

    log.info(
        f"=== enrich: {len(mp4_files)} file(s) | "
        f"platform={effective_platform} | "
        f"tags={settings.min_tags}–{settings.max_tags} ==="
    )
    alert(
        f"Started: {len(mp4_files)} file(s) | platform={effective_platform}",
        settings,
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
    log.info(f"Done. ok={counts['ok']}  skipped={counts['skipped']}  error={counts['error']}")
    log.info("=" * 55)

    alert(
        f"Finished: ok={counts['ok']}  skipped={counts['skipped']}  error={counts['error']}",
        settings,
    )

    return 1 if counts["error"] > 0 else 0


def _dry_run_state(json_path: Path) -> str:
    """Describe what a real run would do with this file, without touching the LLM."""
    if not json_path.exists():
        return "would generate (no sidecar yet)"
    try:
        with open(json_path, encoding="utf-8") as f:
            existing = json.load(f)
    except (json.JSONDecodeError, OSError):
        return "would re-generate (sidecar unreadable)"
    if "hashtags" in existing:
        return "would skip (already enriched)"
    return "would generate (sidecar has no hashtags)"


def main() -> None:
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
