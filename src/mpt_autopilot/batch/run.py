#!/usr/bin/env python3
"""
batch.py — mpt-batch entry point.

Reads a jobs YAML file and generates all pending videos through the
MoneyPrinterTurbo API. Finished videos are tracked in seen.txt and
skipped automatically, so re-running after a crash or Ctrl-C is safe.

Usage:
  batch
  batch --jobs jobs_en.yaml
  batch --config /path/to/config.yaml --jobs jobs_en.yaml
  batch --dry-run
  batch --status
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from mpt_batch.engine import notify, seen, voices
from mpt_batch.engine.api import submit_job, wait_for_task
from mpt_batch.engine.settings import Settings
from mpt_batch.engine.settings import load as load_settings

# ── Logging ───────────────────────────────────────────────────────────────────


def log(msg: str, settings: Settings, *, to_file: bool = True) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    if not to_file:
        return
    log_file = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if log_file.exists() and log_file.stat().st_size > settings.log_max_bytes:
        log_file.unlink()
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"{line}\n")


# ── File handling ─────────────────────────────────────────────────────────────


def copy_result(task_data: dict, output_file: str, settings: Settings) -> str:
    """
    Copy the finished video (and script.json if present) to output_dir.
    Tries the API-reported path first, falls back to the standard task dir.
    Returns task_id.
    """
    task_id: str = task_data["task_id"]
    storage = settings.mpt_storage

    source_video: Path | None = None
    api_candidate: Path | None = None
    if task_data.get("videos"):
        api_candidate = storage / task_data["videos"][0].lstrip("/")
        if api_candidate.exists():
            source_video = api_candidate
            log(f"  using API path: {api_candidate}", settings)

    fallback = storage / "tasks" / task_id / "final-1.mp4"
    if source_video is None:
        if fallback.exists():
            source_video = fallback
            log(f"  using fallback path: {fallback}", settings)
            notify.alert(f"⚠️ Fallback path used for {output_file}", settings)
        else:
            raise FileNotFoundError(
                f"Video not found.\n  API path: {api_candidate}\n  Fallback: {fallback}"
            )

    dest_video = settings.output_dir / output_file
    dest_script = settings.output_dir / Path(output_file).with_suffix(".json")
    source_script = storage / "tasks" / task_id / "script.json"

    shutil.copy2(source_video, dest_video)
    log(f"  saved: {dest_video}", settings)

    if source_script.exists():
        shutil.copy2(source_script, dest_script)
        log(f"  saved: {dest_script}", settings)
    else:
        log(f"  note: no script.json found for {task_id}", settings)

    return task_id


def cleanup_task(task_id: str, settings: Settings) -> None:
    task_dir = settings.mpt_storage / "tasks" / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
        log(f"  removed task dir: {task_id}", settings)


def cleanup_cache(settings: Settings) -> None:
    if not settings.cache_cleanup_enabled:
        return
    cache_dir = settings.mpt_storage / "cache_videos"
    if not cache_dir.exists():
        return
    files = list(cache_dir.glob("*.mp4"))
    for f in files:
        f.unlink(missing_ok=True)
    if files:
        log(f"cache cleaned: {len(files)} file(s) removed", settings)


# ── Single job (with retries) ─────────────────────────────────────────────────


def run_job(job: dict, defaults: dict, voice_pool: dict, settings: Settings) -> bool:
    """Run one job with retry logic. Returns True on success."""
    payload = {
        **defaults,
        **{k: v for k, v in job.items() if k not in ("name", "enabled", "output_file")},
    }
    payload = voices.resolve(payload, voice_pool)

    for attempt in range(1, settings.max_retries + 1):
        task_id: str | None = None
        try:
            log(f"starting: {job['name']} (attempt {attempt}/{settings.max_retries})", settings)
            task_id = submit_job(payload, settings)
            log(f"task_id: {task_id}", settings)
            task_data = wait_for_task(task_id, settings, lambda m: log(m, settings))
            copy_result(task_data, job["output_file"], settings)
            cleanup_task(task_id, settings)
            log(f"done: {job['name']}", settings)
            return True

        except Exception as exc:
            log(f"FAILED {job['name']} (attempt {attempt}/{settings.max_retries}): {exc}", settings)
            if task_id:
                cleanup_task(task_id, settings)
            if attempt < settings.max_retries:
                log(f"retrying in {settings.retry_delay_seconds}s...", settings)
                time.sleep(settings.retry_delay_seconds)

    log(f"all {settings.max_retries} attempts exhausted for: {job['name']}", settings)
    return False


# ── Core logic ────────────────────────────────────────────────────────────────


def run(jobs_path: Path, settings: Settings, *, dry_run: bool) -> None:
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs_cfg = yaml.safe_load(f) or {}

    defaults: dict = jobs_cfg.get("defaults", {})
    jobs: list[dict] = jobs_cfg.get("jobs", [])
    already_seen = seen.load(settings.seen_file)
    voice_pool = voices.load_pool(settings.voices_file)

    log(
        f"=== mpt-batch: {len(jobs)} jobs | {len(already_seen)} already seen ===",
        settings,
        to_file=not dry_run,
    )

    if dry_run:
        print("\n[DRY RUN] Jobs that would run:\n")
        for job in jobs:
            name = job.get("name", job.get("output_file", "?"))
            if not job.get("enabled", True):
                print(f"  ⏸  {name} (disabled)")
                continue
            if job["output_file"] in already_seen:
                print(f"  ✓  {name} (already done)")
                continue
            merged = {**defaults, **{k: v for k, v in job.items() if k != "name"}}
            try:
                voices.resolve(merged, voice_pool)
                print(f"  →  {name}")
            except KeyError as exc:
                print(f"  ✗  {name} — {exc}")
        return

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    notify.alert(f"🚀 Batch started: {len(jobs)} jobs", settings)

    ok: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    consecutive_failures = 0

    for job in jobs:
        name = job.get("name", job.get("output_file", "?"))

        if not job.get("enabled", True):
            skipped.append(f"{name} (disabled)")
            continue

        if seen.contains(settings.seen_file, job["output_file"]):
            skipped.append(f"{name} (already done)")
            log(f"skip: {job['output_file']} (in seen registry)", settings)
            continue

        success = run_job(job, defaults, voice_pool, settings)

        if success:
            ok.append(name)
            consecutive_failures = 0
            seen.add(settings.seen_file, job["output_file"])
            if (
                settings.cache_cleanup_enabled
                and settings.cache_cleanup_interval > 0
                and len(ok) % settings.cache_cleanup_interval == 0
            ):
                log(f"periodic cache cleanup after {len(ok)} videos", settings)
                cleanup_cache(settings)
        else:
            failed.append(name)
            consecutive_failures += 1
            if consecutive_failures >= settings.max_consecutive_failures:
                log(
                    f"ABORT: {consecutive_failures} consecutive failures — "
                    f"API may be down or token expired",
                    settings,
                )
                notify.alert(
                    f"🔴 ABORT: {consecutive_failures} consecutive failures\n"
                    f"API may be down or token expired.",
                    settings,
                )
                break

    cleanup_cache(settings)
    _print_summary(ok, failed, skipped, settings)


def _print_summary(
    ok: list[str],
    failed: list[str],
    skipped: list[str],
    settings: Settings,
) -> None:
    log("=" * 50, settings)
    log(f"DONE  ✓ {len(ok)}  ✗ {len(failed)}  → {len(skipped)}", settings)
    for name in ok:
        log(f"  ✓ {name}", settings)
    for name in failed:
        log(f"  ✗ {name}", settings)
    log("=" * 50, settings)

    icon = "✅" if not failed else "⚠️"
    notify.alert(
        f"{icon} Batch done\n✓ {len(ok)}  ✗ {len(failed)}  → {len(skipped)}",
        settings,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch",
        description="Batch video generator for MoneyPrinterTurbo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    batch
    batch --jobs jobs_en.yaml
    batch --config /path/to/config.yaml --jobs jobs_en.yaml
    batch --dry-run
    batch --status
""",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        metavar="PATH",
        help="Config file path (default: config.yaml)",
    )
    parser.add_argument(
        "--jobs",
        type=Path,
        default=Path("jobs.yaml"),
        metavar="PATH",
        help="Jobs file path (default: jobs.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which jobs would run without generating anything",
    )
    parser.add_argument("--status", action="store_true", help="Show seen registry stats and exit")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        settings = load_settings(args.config)
    except (FileNotFoundError, KeyError) as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    if args.status:
        entries = seen.list_all(settings.seen_file)
        print(f"\nSeen registry: {settings.seen_file}")
        print(f"Total entries: {len(entries)}\n")
        for name in entries:
            print(f"  {name}")
        return

    if not args.jobs.exists():
        print(f"[ERROR] Jobs file not found: {args.jobs}")
        print(f"        Copy jobs.example.yaml → {args.jobs} and add your video topics.")
        sys.exit(1)

    run(args.jobs, settings, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
