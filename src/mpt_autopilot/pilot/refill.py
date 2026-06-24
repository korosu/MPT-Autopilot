#!/usr/bin/env python3
"""
refill.py — shorts-pilot entry point.

Checks how many jobs are pending in jobs_<lang>.yaml and refills
the queue by generating new video ideas via LLM when it runs low.

Usage:
  python refill.py --lang en --jobs-dir /your/path/to/jobs
  python refill.py --lang es --jobs-dir /your/path/to/jobs
  python refill.py --lang en --jobs-dir /your/path/to/jobs --force
  python refill.py --lang en --jobs-dir /your/path/to/jobs --count 50
  python refill.py --lang en --jobs-dir /your/path/to/jobs --threshold 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from generator import jobs, seen
from generator.seen import load_ordered as seen_load_ordered
from generator.llm import call_llm, parse_json_array
from generator.prompt import VIDEO_SUBJECT_MAX_CHARS, build as build_prompt
from generator.settings import load as load_settings


# ── Deduplication + cleanup ───────────────────────────────────────────────────

def _normalise(job: dict, expected_suffix: str) -> dict:
    """
    Return a cleaned copy of job with:
    - enabled forced to True
    - path separators stripped from output_file
    - missing file_suffix corrected on output_file
    - video_subject clamped to VIDEO_SUBJECT_MAX_CHARS
    """
    output_file = job.get("output_file", "")

    # Strip any accidental path components (e.g. "subdir/fact.mp4" → "fact.mp4")
    output_file = Path(output_file).name

    # Ensure the correct suffix is present
    if expected_suffix and not output_file.lower().endswith(f"{expected_suffix}.mp4"):
        stem = output_file.removesuffix(".mp4")
        output_file = f"{stem}{expected_suffix}.mp4"

    subject = job.get("video_subject", "")
    if len(subject) > VIDEO_SUBJECT_MAX_CHARS:
        subject = subject[:VIDEO_SUBJECT_MAX_CHARS]

    return {
        **job,
        "output_file": output_file,
        "enabled": True,
        "video_subject": subject,
    }


def _deduplicate(
    raw_jobs: list[dict],
    already_known: set[str],
    expected_suffix: str = "",
) -> list[dict]:
    known = set(already_known)
    result = []
    for job in raw_jobs:
        output_file = job.get("output_file", "")
        if not output_file:
            print(f"  [skip] job missing output_file: {job.get('name', '?')}")
            continue
        # Normalise first so we dedup against the corrected filename
        clean = _normalise(job, expected_suffix)
        output_file = clean["output_file"]
        if output_file in known:
            print(f"  [skip duplicate] {output_file}")
            continue
        if not clean.get("name"):
            clean = {**clean, "name": jobs.safe_name(output_file.replace(".mp4", ""))}
        known.add(output_file)
        result.append(clean)
    return result


# ── Core logic ────────────────────────────────────────────────────────────────

def run(
    lang: str,
    jobs_dir: Path,
    seen_dir: Path,
    force: bool,
    count_override: int | None,
    threshold_override: int | None,
) -> int:
    settings = load_settings()
    lang_cfg = settings.lang(lang)

    suffix = lang_cfg.file_suffix
    seen_set = seen.load(seen_dir, suffix)
    seen_list = seen_load_ordered(seen_dir, suffix)

    threshold = threshold_override if threshold_override is not None else settings.refill_threshold
    generate_count = count_override if count_override is not None else settings.generate_count

    # Load YAML once; derive both pending count and existing names from it.
    cfg = jobs.load(jobs_dir, lang)
    pending = jobs.count_pending_from(cfg, seen_set)
    seen_file = "seen.txt" if not suffix else f"seen_{suffix.lstrip('_')}.txt"
    print(f"[{lang}] pending jobs: {pending} | threshold: {threshold} | seen file: {seen_file}")

    if pending >= threshold and not force:
        print(f"[{lang}] Queue is full — nothing to do. (Use --force to override.)")
        return 0

    existing = jobs.existing_names_from(cfg)
    already_known = seen_set | existing
    print(f"[{lang}] known titles: {len(already_known)} | model: {settings.model}")

    system_prompt, user_prompt = build_prompt(lang_cfg, already_known, generate_count, seen_ordered=seen_list)
    print(f"[{lang}] calling LLM for {generate_count} ideas...")

    raw_text = call_llm(system_prompt, user_prompt, settings)
    raw_jobs = parse_json_array(raw_text)

    if len(raw_jobs) < generate_count:
        print(f"[{lang}] WARNING: LLM returned {len(raw_jobs)} of {generate_count} requested — queue may still be low after this run")

    print(f"[{lang}] LLM returned {len(raw_jobs)} raw jobs")

    clean_jobs = _deduplicate(raw_jobs, already_known, expected_suffix=suffix)
    print(f"[{lang}] after dedup: {len(clean_jobs)} new jobs")

    if not clean_jobs:
        print(f"[{lang}] nothing new after dedup — try again or use --force")
        return 0

    jobs.append(jobs_dir, lang, clean_jobs)
    print(f"[{lang}] appended {len(clean_jobs)} jobs to jobs_{lang}.yaml")
    # Note: seen.txt is updated by batch_generate.py after each video is rendered,
    # not here — refill.py only writes to the jobs yaml.
    return len(clean_jobs)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="refill.py",
        description="Auto-refill your MoneyPrinterTurbo jobs queue with LLM-generated video ideas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python refill.py --lang en --jobs-dir /your/path/to/jobs
  python refill.py --lang es --jobs-dir /your/path/to/jobs
  python refill.py --lang en --jobs-dir /your/path/to/jobs --force
  python refill.py --lang en --jobs-dir /your/path/to/jobs --count 50
  python refill.py --lang en --jobs-dir /your/path/to/jobs --threshold 5
""",
    )
    parser.add_argument("--lang", required=True, metavar="LANG",
        help="Language code (e.g. en, es). Must be defined in config.yaml.")
    parser.add_argument("--jobs-dir", type=Path, default=Path("."), metavar="PATH",
        help="Directory containing jobs_<lang>.yaml files. Default: current directory.")
    parser.add_argument("--seen-dir", type=Path, default=None, metavar="PATH",
        help="Directory for seen_<lang>.txt files. Default: same as --jobs-dir.")
    parser.add_argument("--force", action="store_true",
        help="Refill even if the queue is above the threshold.")
    parser.add_argument("--count", type=int, default=None, metavar="N",
        help="Override generation.count from config.yaml.")
    parser.add_argument("--threshold", type=int, default=None, metavar="N",
        help="Override generation.threshold from config.yaml.")

    args = parser.parse_args()

    if args.count is not None and args.count <= 0:
        print("[ERROR] --count must be a positive integer")
        sys.exit(1)
    if args.threshold is not None and args.threshold < 0:
        print("[ERROR] --threshold must be a non-negative integer")
        sys.exit(1)

    jobs_dir = args.jobs_dir.resolve()
    seen_dir = (args.seen_dir or jobs_dir).resolve()

    if not jobs_dir.is_dir():
        print(f"[ERROR] jobs directory not found: {jobs_dir}")
        sys.exit(1)

    try:
        added = run(
            lang=args.lang,
            jobs_dir=jobs_dir,
            seen_dir=seen_dir,
            force=args.force,
            count_override=args.count,
            threshold_override=args.threshold,
        )
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    print(f"\n[done] added {added} new jobs.")


if __name__ == "__main__":
    main()
