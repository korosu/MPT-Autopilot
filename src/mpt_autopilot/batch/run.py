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
    """
    Print + append to log_file. Rotation is simple deletion, not archiving:
    once the file exceeds log_max_bytes, it's discarded entirely and a fresh
    one starts — there's no log.1/.log.2 history kept. Fine for a batch
    runner where the point is "don't fill the disk", not long-term audit.
    """
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    if not to_file:
        return
    log_file = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if log_file.exists() and log_file.stat().st_size > settings.log_max_bytes:
        log_file.unlink()  # delete-and-restart, not archive — see docstring
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
        if api_candidate and api_candidate.exists():
            source_video = api_candidate
            log(f"  using API path: {api_candidate}", settings)

    fallback = storage / "tasks" / task_id / "final-1.mp4"
    if source_video is None:
        if fallback.exists():
            source_video = fallback
            log(f"  using fallback path: {fallback}", settings)
            notify.alert(
                f"Fallback path used for '{output_file}' (task_id={task_id})\n"
                f"API-reported video path was missing; used the default task "
                f"directory instead: {fallback}",
                settings,
            )
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
    """
    Run one job with retry logic. Returns True on success, False if all
    retries are exhausted — never raises, so one bad job can't crash the batch.

    Retries exist for transient failures (a dropped connection, MPT briefly
    unavailable, a one-off broken/stuck task from api.wait_for_task) — the
    kind of thing that's likely to succeed on a second or third try. They do
    nothing for a persistent problem (bad credentials, MPT genuinely down);
    that's what max_consecutive_failures in run() is for — it aborts the
    whole batch instead of retrying every single job into the same wall.
    """
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


def run(
    jobs_path: Path, settings: Settings, *, dry_run: bool, seen_override: Path | None = None
) -> None:
    with open(jobs_path, encoding="utf-8") as f:
        jobs_cfg = yaml.safe_load(f) or {}

    defaults: dict = jobs_cfg.get("defaults", {})
    jobs: list[dict] = jobs_cfg.get("jobs", [])
    seen_file = seen_override or settings.seen_file
    already_seen = seen.load(seen_file)
    voice_pool = settings.voice_pool

    # Warn if defaults section is missing critical fields
    if not defaults:
        log(
            "WARNING: jobs file has no 'defaults:' section — "
            "videos may render with wrong language/aspect/subtitles",
            settings,
        )
    elif "video_language" not in defaults:
        log(
            "WARNING: 'defaults:' missing 'video_language' — "
            "output language may default incorrectly",
            settings,
        )

    disabled_count = sum(1 for j in jobs if not j.get("enabled", True))
    already_done_count = sum(
        1 for j in jobs if j.get("enabled", True) and j.get("output_file") in already_seen
    )
    to_run_count = len(jobs) - disabled_count - already_done_count

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
                print(f"  disabled  {name}")
                continue
            if job["output_file"] in already_seen:
                print(f"  done      {name} (already done)")
                continue
            merged = {**defaults, **{k: v for k, v in job.items() if k != "name"}}
            try:
                voices.resolve(merged, voice_pool)
                print(f"  run       {name}")
            except KeyError as exc:
                print(f"  error     {name} - {exc}")
        return

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    start_time = time.time()
    notify.alert(
        f"Batch started: {jobs_path.name}\n"
        f"Total jobs: {len(jobs)}  To run: {to_run_count}  "
        f"Already done: {already_done_count}  Disabled: {disabled_count}",
        settings,
    )

    ok: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    consecutive_failures = 0

    for index, job in enumerate(jobs, start=1):
        name = job.get("name", job.get("output_file", "?"))

        if not job.get("enabled", True):
            skipped.append(f"{name} (disabled)")
            continue

        if seen.contains(seen_file, job["output_file"]):
            skipped.append(f"{name} (already done)")
            log(f"skip: {job['output_file']} (in seen registry)", settings)
            continue

        success = run_job(job, defaults, voice_pool, settings)

        if success:
            ok.append(name)
            consecutive_failures = 0
            seen.add(seen_file, job["output_file"])
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
                    f"Batch aborted: {consecutive_failures} consecutive failures "
                    f"(last: {name})\n"
                    f"Progress: {index}/{len(jobs)} jobs processed — "
                    f"{len(ok)} succeeded, {len(failed)} failed so far.\n"
                    f"Likely cause: API unreachable or an invalid/expired token. "
                    f"Remaining jobs were not attempted.",
                    settings,
                )
                break

    cleanup_cache(settings)
    _print_summary(ok, failed, skipped, settings, started_at=start_time)


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _print_summary(
    ok: list[str],
    failed: list[str],
    skipped: list[str],
    settings: Settings,
    *,
    started_at: float,
) -> None:
    duration = _format_duration(time.time() - started_at)

    log("=" * 50, settings)
    log(
        f"DONE  ok={len(ok)}  failed={len(failed)}  skipped={len(skipped)}  duration={duration}",
        settings,
    )
    for name in ok:
        log(f"  ok: {name}", settings)
    for name in failed:
        log(f"  failed: {name}", settings)
    log("=" * 50, settings)

    status = "all succeeded" if not failed else f"{len(failed)} failed"
    lines = [
        f"Batch finished ({status})",
        f"Duration: {duration}",
        f"Succeeded: {len(ok)}  Failed: {len(failed)}  Skipped: {len(skipped)}",
    ]
    if failed:
        lines.append("Failed jobs: " + ", ".join(failed))
    notify.alert("\n".join(lines), settings)


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
    batch --list-voices es

    # Multi-language: override seen file to match shorts-pilot's per-lang seen files
    batch --jobs jobs_es.yaml --seen seen_es.txt
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
    parser.add_argument(
        "--list-voices",
        nargs="?",
        const="",
        default=None,
        metavar="FILTER",
        help=(
            "List available voice aliases (bundled Edge TTS voices + your "
            "config.yaml presets) and exit. Optionally filter by a substring, "
            "e.g. `--list-voices es` or `--list-voices gemini`."
        ),
    )
    parser.add_argument(
        "--seen",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Override seen_file from config.yaml (e.g. --seen seen_es.txt for "
            "multi-language setups using shorts-pilot)."
        ),
    )
    return parser


def list_voices(settings: Settings, filter_str: str) -> None:
    filter_str = filter_str.lower().strip()
    matches = {
        alias: fields
        for alias, fields in sorted(settings.voice_pool.items())
        if filter_str in alias.lower() or filter_str in fields.get("voice_name", "").lower()
    }
    if not matches:
        print(f"No voices matched '{filter_str}'.")
        return
    suffix = f" matching '{filter_str}'" if filter_str else ""
    print(f"{len(matches)} voice(s){suffix}:\n")
    for alias, fields in matches.items():
        print(f"  {alias:<40} voice_name={fields.get('voice_name', '?')}")
    print('\nUse in jobs.yaml as:  voice: "<alias>"')


def main() -> None:
    args = build_parser().parse_args()

    try:
        settings = load_settings(args.config)
    except (FileNotFoundError, KeyError) as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # Resolve --seen path relative to config.yaml's location (same as seen_file)
    seen_override: Path | None = None
    if args.seen is not None:
        cfg_dir = args.config.resolve().parent
        seen_override = cfg_dir / args.seen if not args.seen.is_absolute() else args.seen

    if args.status:
        seen_path = seen_override or settings.seen_file
        entries = seen.list_all(seen_path)
        print(f"\nSeen registry: {seen_path}")
        print(f"Total entries: {len(entries)}\n")
        for name in entries:
            print(f"  {name}")
        return

    if args.list_voices is not None:
        list_voices(settings, args.list_voices)
        return

    if not args.jobs.exists():
        print(f"[ERROR] Jobs file not found: {args.jobs}")
        print(f"        Copy jobs.example.yaml to {args.jobs} and add your video topics.")
        sys.exit(1)

    run(args.jobs, settings, dry_run=args.dry_run, seen_override=seen_override)


if __name__ == "__main__":
    main()
