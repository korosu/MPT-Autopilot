#!/usr/bin/env python3
"""
batch/run.py — batch stage entry point (`mpt batch`).

Reads a jobs YAML file and generates all pending videos through the
MoneyPrinterTurbo API. Finished videos are tracked in seen.txt and
skipped automatically, so re-running after a crash or Ctrl-C is safe.

Usage:
  mpt batch
  mpt batch --jobs jobs_en.yaml
  mpt --config /path/to/config.yaml batch --jobs jobs_en.yaml
  mpt batch --dry-run
  mpt batch --status
  mpt batch --list-bgm
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from mpt_autopilot import config as shared_config
from mpt_autopilot import notify, seen
from mpt_autopilot.batch import bgm, state, voices
from mpt_autopilot.batch.api import health_check, submit_job, wait_for_task
from mpt_autopilot.batch.settings import Settings
from mpt_autopilot.batch.settings import load as load_settings
from mpt_autopilot.batch.upload_post import (
    UploadPostError,
    UploadPostUnavailable,
    upload_video,
)
from mpt_autopilot.config import ConfigError
from mpt_autopilot.uploader.metadata import title_from_filename

# ── Logging ───────────────────────────────────────────────────────────────────


def log(msg: str, settings: Settings, *, to_file: bool = True) -> None:
    """
    Print + append to log_file. One backup copy (log_file.1) kept on rotation.
    """
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    if not to_file:
        return
    log_file = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if log_file.exists() and log_file.stat().st_size > settings.log_max_bytes:
        backup = log_file.with_suffix(log_file.suffix + ".1")
        backup.unlink(missing_ok=True)
        log_file.rename(backup)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"{line}\n")


# ── File handling ─────────────────────────────────────────────────────────────


def _task_dir(task_id: object, settings: Settings) -> Path:
    """
    Resolve and containment-check the task directory for a task_id.

    task_id is untrusted: it arrives from the MPT API response, and on the
    crash-recovery path from in_progress.txt (a file we persist ourselves but
    that outlives the process that wrote it). Both are interpolated into
    filesystem paths here and in cleanup_task(), which calls shutil.rmtree().
    Without this check a task_id of ".." resolves to mpt_storage itself and
    deletes the entire storage tree.

    Raises ValueError on anything that is not a safe single path segment or
    that would escape mpt_storage/tasks/.
    """
    from mpt_autopilot.batch.api import validate_task_id

    safe = validate_task_id(task_id)
    tasks_root = settings.mpt_storage / "tasks"
    task_dir = tasks_root / safe
    if not task_dir.resolve().is_relative_to(tasks_root.resolve()):
        raise ValueError(
            f"refusing to use task_id {safe!r}: it resolves to {task_dir}, "
            f"which is outside {tasks_root}"
        )
    return task_dir


def _output_dest(output_file: object, settings: Settings) -> tuple[Path, Path]:
    """
    Resolve the destination paths for output_file inside settings.output_dir.

    output_file comes from jobs.yaml, which the user edits by hand (the pilot
    stage sanitises its own output, but a hand-written or hand-imported entry
    does not get that treatment). Returns (video, script) paths, both
    guaranteed to stay inside output_dir.

    Raises ValueError on absolute paths, parent references, or anything that
    resolves outside output_dir — otherwise shutil.copy2() would overwrite
    whatever the process can write to, including config.yaml or accounts.yaml.
    """
    if not isinstance(output_file, str) or not output_file.strip():
        raise ValueError(f"output_file must be a non-empty string, got {output_file!r}")
    # Normalise backslashes so path-traversal checks work cross-platform
    # (a Windows-style "..\\.." should be caught even when running on Linux).
    output_file = output_file.replace("\\", "/")
    if Path(output_file).is_absolute() or re.match(r"^[A-Za-z]:/", output_file):
        raise ValueError(f"refusing absolute output_file {output_file!r}")
    if ".." in Path(output_file).parts:
        raise ValueError(f"refusing output_file with parent reference: {output_file!r}")

    out_dir = settings.output_dir
    dest_video = (out_dir / output_file).resolve()
    if not dest_video.parent == out_dir.resolve():
        raise ValueError(
            f"refusing output_file {output_file!r}: it resolves to {dest_video}, "
            f"which is outside {out_dir}"
        )
    return dest_video, dest_video.with_suffix(".json")


def copy_result(task_data: dict, output_file: str, settings: Settings) -> tuple[str, Path]:
    """
    Copy the finished video (and script.json if present) to output_dir.
    Returns (task_id, dest_video_path).
    """
    task_id: str = task_data["task_id"]
    task_dir = _task_dir(task_id, settings)
    storage = settings.mpt_storage

    source_video: Path | None = None
    api_candidate: Path | None = None

    # 1. Try the API-reported "videos" list (single-clip tasks).
    if task_data.get("videos"):
        video_path = task_data["videos"][0].lstrip("/")
        if video_path:
            candidate = storage / video_path
            # The API-reported path is untrusted input, like task_id; a value
            # containing a parent reference would read from outside mpt_storage.
            if candidate.resolve().is_relative_to(storage.resolve()) and candidate.is_file():
                source_video = candidate
                api_candidate = candidate
                log(f"  using API path: {api_candidate}", settings)

    # 2. Fall back to "combined_videos" (multi-clip concatenated tasks).
    if source_video is None and task_data.get("combined_videos"):
        cv_path = task_data["combined_videos"][0].lstrip("/")
        if cv_path:
            candidate = storage / cv_path
            if candidate.resolve().is_relative_to(storage.resolve()) and candidate.is_file():
                source_video = candidate
                api_candidate = candidate
                log(f"  using combined_videos API path: {api_candidate}", settings)

    # 3. Final fallback: the well-known task-dir output.
    fallback = task_dir / "final-1.mp4"
    if source_video is None:
        if fallback.exists():
            source_video = fallback
            log(f"  using fallback path: {fallback}", settings)
            # Absolute path stays in the local log only — Telegram must not carry
            # filesystem paths (machine layout) or the raw task_id (attacker
            # influenced from the MPT API response).
            notify.alert(
                f"Fallback path used for '{output_file}' (task_id={task_id})\n"
                f"API-reported video path was missing; used the default task "
                f"directory tasks/{task_id}/final-1.mp4 instead.",
                settings,
            )
        else:
            raise FileNotFoundError(
                f"Video not found.\n  API path: {api_candidate}\n  Fallback: {fallback}"
            )

    dest_video, dest_script = _output_dest(output_file, settings)
    source_script = task_dir / "script.json"

    shutil.copy2(source_video, dest_video)
    log(f"  saved: {dest_video}", settings)

    if source_script.exists():
        shutil.copy2(source_script, dest_script)
        log(f"  saved: {dest_script}", settings)
    else:
        log(f"  note: no script.json found for {task_id}", settings)

    # Copy subtitle files if present
    for pattern in ["*.srt", "*.ass"]:
        for sub_file in task_dir.glob(pattern):
            dest_sub = (settings.output_dir / sub_file.name).resolve()
            if not dest_sub.parent == settings.output_dir.resolve():
                raise ValueError(f"refusing to copy subtitle outside output_dir: {sub_file}")
            shutil.copy2(sub_file, dest_sub)
            log(f"  saved: {dest_sub}", settings)

    return task_id, dest_video


def cleanup_task(task_id: str, settings: Settings) -> None:
    """Remove a finished task's directory. Refuses ids that escape mpt_storage."""
    task_dir = _task_dir(task_id, settings)
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


def run_job(
    job: dict, defaults: dict, voice_pool: dict, settings: Settings, in_progress_path: Path
) -> bool:
    """
    Run one job with retry logic. Returns True on success, False if all
    retries are exhausted — never raises, so one bad job can't crash the batch.

    Retries exist for transient failures (a dropped connection, MPT briefly
    unavailable, a one-off broken/stuck task from api.wait_for_task) — the
    kind of thing that's likely to succeed on a second or third try. They do
    nothing for a persistent problem (bad credentials, MPT genuinely down);
    that's what max_consecutive_failures in run() is for — it aborts the
    whole batch instead of retrying every single job into the same wall.

    Each submitted task_id is persisted to in_progress_path immediately so a
    crash or Ctrl-C mid-poll can resume the existing task on restart instead
    of creating a duplicate.

    Per-job max_retries / retry_delay_seconds override the global settings,
    implementing the "configurable retries per job" feature.
    """
    _meta_keys = ("name", "enabled", "output_file", "max_retries", "retry_delay_seconds")
    payload = {
        **defaults,
        **{k: v for k, v in job.items() if k not in _meta_keys},
    }
    payload = voices.resolve(payload, voice_pool)

    max_retries = int(job.get("max_retries", settings.max_retries))
    retry_delay = int(job.get("retry_delay_seconds", settings.retry_delay_seconds))

    for attempt in range(1, max_retries + 1):
        task_id: str | None = None
        try:
            log(f"starting: {job['name']} (attempt {attempt}/{max_retries})", settings)
            task_id = submit_job(payload, settings)
            state.add(in_progress_path, job["output_file"], task_id, attempt)
            log(f"task_id: {task_id}", settings)
            task_data = wait_for_task(task_id, settings, lambda m: log(m, settings))
            copy_task_id, dest_video = copy_result(task_data, job["output_file"], settings)
            cleanup_task(copy_task_id, settings)
            state.remove(in_progress_path, job["output_file"])

            # ── upload-post (optional) ────────────────────────────────────────
            if settings.upload_post_enabled:
                try:
                    sidecar = dest_video.with_suffix(".json")
                    meta_title = dest_video.stem
                    meta_desc = ""
                    meta_tags: list[str] = []
                    if sidecar.exists():
                        raw = json.loads(sidecar.read_text(encoding="utf-8"))
                        meta_title = raw.get("title", dest_video.stem)
                        meta_desc = raw.get("description", "")
                        meta_tags = raw.get("tags", [])
                    else:
                        meta_title = title_from_filename(dest_video)
                    upload_video(
                        video_path=dest_video,
                        meta_title=meta_title,
                        meta_description=meta_desc,
                        meta_tags=meta_tags,
                        settings=settings,
                        privacy_status=settings.upload_post_youtube_privacy_status,
                        platforms=settings.upload_post_platforms,
                        log=lambda m: log(m, settings),
                    )
                except (UploadPostUnavailable, UploadPostError) as exc:
                    log(f"upload-post failed for {job['name']}: {exc}", settings)

            log(f"done: {job['name']}", settings)
            return True

        except Exception as exc:
            log(f"FAILED {job['name']} (attempt {attempt}/{max_retries}): {exc}", settings)
            if task_id:
                cleanup_task(task_id, settings)
            if attempt < max_retries:
                state.remove(in_progress_path, job["output_file"])
                log(f"retrying in {retry_delay}s...", settings)
                time.sleep(retry_delay)

    state.remove(in_progress_path, job["output_file"])
    log(f"all {max_retries} attempts exhausted for: {job['name']}", settings)
    return False


# ── Core logic ────────────────────────────────────────────────────────────────


def run(
    jobs_path: Path, settings: Settings, *, dry_run: bool, seen_override: Path | None = None
) -> int:
    with open(jobs_path, encoding="utf-8") as f:
        jobs_cfg = yaml.safe_load(f) or {}

    defaults: dict = jobs_cfg.get("defaults", {})
    jobs: list[dict] = jobs_cfg.get("jobs", [])
    seen_file = seen_override or settings.seen_file
    already_seen = seen.load(seen_file)
    voice_pool = settings.voice_pool
    in_progress_path = seen_file.with_name(seen_file.stem + ".in_progress.txt")

    # Resume in-progress tasks from a previous interrupted run
    try:
        pending = state.list_all(in_progress_path)
    except ValueError as exc:
        # A corrupt line in in_progress.txt would otherwise surface as a bare
        # traceback. It means a submitted job's task_id is unrecoverable, so
        # the run must stop — resuming anything else could double-submit.
        log(f"ERROR: {exc}", settings)
        notify.alert(
            f"Batch aborted before start: {in_progress_path.name} is corrupt.\n"
            f"{exc}\nNo jobs were submitted.",
            settings,
        )
        return 1
    if pending:
        log(f"{len(pending)} in-progress task(s) found — attempting resume", settings)
        for entry in pending:
            output_file: str = entry["output_file"]
            task_id: str = entry["task_id"]
            if output_file in already_seen:
                state.remove(in_progress_path, output_file)
                continue
            job = next((j for j in jobs if j.get("output_file") == output_file), None)
            if not job:
                log(f"orphan in-progress entry '{output_file}' (not in jobs file)", settings)
                state.remove(in_progress_path, output_file)
                continue
            name = job.get("name", output_file)
            log(f"resuming: {name} (task_id={task_id})", settings)
            try:
                task_data = wait_for_task(task_id, settings, lambda m: log(m, settings))
                copy_result(task_data, output_file, settings)
                cleanup_task(task_id, settings)
                seen.add(seen_file, output_file)
                state.remove(in_progress_path, output_file)
                log(f"resumed and done: {output_file}", settings)
            except Exception as exc:
                log(
                    f"resume failed for '{output_file}' (task_id={task_id}): {exc} — "
                    f"will re-submit as new task",
                    settings,
                )
                state.remove(in_progress_path, output_file)
                try:
                    cleanup_task(task_id, settings)
                except Exception:
                    pass

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

    # ponytail: O(n) via Counter instead of O(n²) count-per-element
    output_files = [j.get("output_file", "") for j in jobs if j.get("enabled", True)]
    dupes = sorted({f for f, c in Counter(output_files).items() if c > 1})
    if dupes:
        log(
            f"WARNING: duplicate output_file values in jobs.yaml: {', '.join(dupes)} — "
            "first job wins, later jobs will be skipped as 'already done'",
            settings,
        )

    disabled_count = sum(1 for j in jobs if not j.get("enabled", True))
    already_done_count = sum(
        1 for j in jobs if j.get("enabled", True) and j.get("output_file") in already_seen
    )
    to_run_count = len(jobs) - disabled_count - already_done_count

    log(
        f"=== batch: {len(jobs)} jobs | {len(already_seen)} already seen ===",
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
        return 0

    # ── Pre-flight checks ───────────────────────────────────────────────────
    if not health_check(settings):
        log("ERROR: MPT API unreachable at " + settings.api_url, settings)
        notify.alert(
            f"Batch aborted before start: MPT API {settings.api_url} unreachable.\n"
            "No jobs were submitted.",
            settings,
        )
        return 1

    lock_path = seen_file.with_name(seen_file.stem + ".lock")
    _lock_timeout = 1800  # ponytail: 30 min stale lock timeout
    try:
        os.close(os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        try:
            mtime = lock_path.stat().st_mtime
            if time.time() - mtime > _lock_timeout:
                log(
                    f"Stale lock file (age {time.time() - mtime:.0f}s > {_lock_timeout}s) — "
                    f"overwriting",
                    settings,
                )
                os.close(os.open(str(lock_path), os.O_CREAT | os.O_TRUNC | os.O_WRONLY))
            else:
                log(
                    f"ERROR: Lock file exists at {lock_path} — another batch may be running. "
                    f"If not, delete it and re-run.",
                    settings,
                )
                return 1
        except OSError:
            log(
                f"ERROR: Lock file exists at {lock_path} and cannot be read. "
                f"Delete manually if stale.",
                settings,
            )
            return 1

    # ── Lock cleanup on Ctrl+C / SIGTERM / exceptions ──
    # Installed before anything that can raise after the marker was created:
    # an exception between lock creation and the try/finally below (e.g. a
    # PermissionError from output_dir.mkdir) would otherwise leave the marker
    # behind until the 30-minute stale timeout.
    _lock_cleaned_up = False

    def _cleanup_lock() -> None:
        nonlocal _lock_cleaned_up
        if not _lock_cleaned_up:
            lock_path.unlink(missing_ok=True)
            _lock_cleaned_up = True

    def _on_signal(signum: int, _frame: object) -> None:  # _frame unused but required by signal API
        _cleanup_lock()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    _prev_sigint = signal.signal(signal.SIGINT, _on_signal)
    try:
        _prev_sigterm = signal.signal(signal.SIGTERM, _on_signal)
    except AttributeError:
        _prev_sigterm = None  # ponytail: Windows has no SIGTERM

    try:
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
        last_cleanup_at = 0

        for index, job in enumerate(jobs, start=1):
            name = job.get("name", job.get("output_file", "?"))

            if not job.get("enabled", True):
                skipped.append(f"{name} (disabled)")
                continue

            if seen.contains(seen_file, job["output_file"]):
                skipped.append(f"{name} (already done)")
                log(f"skip: {job['output_file']} (in seen registry)", settings)
                continue

            success = run_job(job, defaults, voice_pool, settings, in_progress_path)

            if success:
                ok.append(name)
                consecutive_failures = 0
                seen.add(seen_file, job["output_file"])
                if not _lock_cleaned_up:
                    os.utime(lock_path, None)  # ponytail: keepalive so lock doesn't look stale
                if (
                    settings.cache_cleanup_enabled
                    and settings.cache_cleanup_interval > 0
                    and len(ok) % settings.cache_cleanup_interval == 0
                ):
                    log(f"periodic cache cleanup after {len(ok)} videos", settings)
                    cleanup_cache(settings)
                    last_cleanup_at = len(ok)
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

        if settings.cache_cleanup_enabled and len(ok) != last_cleanup_at:
            cleanup_cache(settings)
    finally:
        _cleanup_lock()
        signal.signal(signal.SIGINT, _prev_sigint)
        if _prev_sigterm is not None:
            signal.signal(signal.SIGTERM, _prev_sigterm)

    return _print_summary(ok, failed, skipped, settings, started_at=start_time)


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
) -> int:
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

    if ok and failed:
        log(
            f"[batch: partial — {len(ok)} ok, {len(failed)} failed, continuing to enrich/upload]",
            settings,
        )
        return 2
    return 1 if failed else 0


# ── CLI ───────────────────────────────────────────────────────────────────────


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register the batch stage's flags on `parser`.

    Kept separate from build_parser() so `mpt`'s subparser and a standalone
    parser share one definition — the CLI owns `--config` at the root level, so
    it is deliberately not registered here.
    """
    parser.add_argument(
        "--jobs",
        type=Path,
        default=None,
        metavar="PATH",
        help="Jobs file path (default: jobs.yaml, or jobs_{suffix}.yaml with --lang)",
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
        "--list-bgm",
        nargs="?",
        const="",
        default=None,
        metavar="FILTER",
        help=(
            "List available background music files in MPT's resource/songs/ and exit. "
            "Optionally filter by filename substring."
        ),
    )
    parser.add_argument(
        "--upload-bgm",
        nargs="?",
        const=".",
        default=None,
        metavar="PATH",
        help=(
            "Copy .mp3 files to MPT's resource/songs/. "
            "Optionally specify source directory (default: current directory)."
        ),
    )
    parser.add_argument(
        "--upload-post",
        nargs="?",
        const=True,
        default=None,
        metavar="BOOL",
        help=(
            "Override batch.upload_post.enabled for this run (true|false). "
            "Use --upload-post without a value to force-enable, even if "
            "config.yaml disables it."
        ),
    )
    parser.add_argument(
        "--seen",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Override batch.seen_file from config.yaml (e.g. --seen seen_es.txt for "
            "multi-language setups driven by `mpt refill`)."
        ),
    )
    parser.add_argument(
        "--lang",
        type=str,
        default=None,
        metavar="CODE",
        help=(
            "Language code matching a key in config.yaml's langs: section. "
            "Derives --jobs, --seen, and output_dir with the language's file_suffix. "
            "E.g. --lang es → jobs_es.yaml, seen_es.txt, exports_es/"
        ),
    )
    return parser


EPILOG = """
Examples:
    mpt batch
    mpt batch --jobs jobs_en.yaml
    mpt --config /path/to/config.yaml batch --jobs jobs_en.yaml
    mpt batch --dry-run
    mpt batch --status
    mpt batch --list-voices es
    mpt batch --list-bgm

    # Multi-language: override the seen file to match per-language seen files
    mpt batch --jobs jobs_es.yaml --seen seen_es.txt
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt batch",
        description="Batch video generator for MoneyPrinterTurbo.",
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
        extras = []
        if "voice_rate" in fields:
            extras.append(f"rate={fields['voice_rate']}")
        if "voice_volume" in fields:
            extras.append(f"volume={fields['voice_volume']}")
        extra_str = "  " + ", ".join(extras) if extras else ""
        print(f"  {alias:<40} voice_name={fields.get('voice_name', '?')}{extra_str}")
    print('\nUse in jobs.yaml as:  voice: "<alias>"')


def list_bgm_cmd(settings: Settings, filter_str: str) -> None:
    songs = bgm.list_bgm_files(settings.mpt_songs_dir, filter_str)
    suffix = f" matching '{filter_str}'" if filter_str else ""
    print(f"{len(songs)} BGM file(s){suffix}:\n")
    for name, size in songs:
        print(f"  {name:<30} {size // 1024} KB")
    print('\nUse in jobs.yaml as:  bgm_type: "custom"  bgm_file: "<name>"  bgm_volume: 0.2')


def upload_bgm_cmd(settings: Settings, source_dir: Path) -> None:
    source_path = source_dir.expanduser().resolve()
    songs_dir = settings.mpt_songs_dir

    if not source_path.exists():
        print(f"[ERROR] Source directory not found: {source_path}")
        return

    songs_dir.mkdir(parents=True, exist_ok=True)
    mp3_files = list(source_path.glob("*.mp3"))

    if not mp3_files:
        print(f"No .mp3 files found in {source_path}")
        return

    copied = 0
    for f in mp3_files:
        shutil.copy2(f, songs_dir / f.name)
        print(f"  copied: {f.name}")
        copied += 1

    print(f"\nCopied {copied} file(s) to {songs_dir}")


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    """
    Run the batch stage from already-parsed arguments.

    `config_path` comes from the root `mpt --config` flag; when it is None the
    shared loader falls back to ./config.yaml. Returns an exit code instead of
    calling sys.exit so `mpt run` can aggregate stages in one process.
    """
    if config_path is None:
        config_path = getattr(args, "config", None)

    try:
        settings = load_settings(config_path)
    except (FileNotFoundError, KeyError, ConfigError) as e:
        print(f"[ERROR] {e}")
        return 1

    # Configure seen.txt rotation threshold from config.yaml.
    from mpt_autopilot import seen as seen_registry

    seen_registry.set_rotation_max_bytes(settings.seen_max_mb * 1024 * 1024)

    # ── CLI overrides ────────────────────────────────────────────────────
    if getattr(args, "upload_post", None) is not None:
        val = str(args.upload_post).lower()
        settings.upload_post_enabled = val in ("true", "1", "yes", "on")

    cfg_dir = shared_config.resolve_path(config_path).resolve().parent

    # Resolve language suffix from --lang
    lang_suffix = ""
    if args.lang is not None:
        if args.lang not in settings.langs:
            print(
                f"[ERROR] Unknown language '{args.lang}'. "
                f"Available in config.yaml langs: {list(settings.langs) or '(none)'}"
            )
            return 1
        lang_suffix = settings.langs[args.lang].get("file_suffix", f"_{args.lang}")

    # Resolve --jobs default: config's jobs > jobs_dir > cwd-relative (with --lang suffix)
    jobs_path: Path | None = args.jobs
    if jobs_path is None:
        # Priority: 1) settings.jobs (direct path), 2) jobs_dir/lang_suffix, 3) cwd/jobs.yaml
        if settings.jobs:
            jobs_path = settings.jobs
        else:
            base_dir = settings.jobs_dir or Path.cwd()
            jobs_path = base_dir / f"jobs{lang_suffix}.yaml"

    # Resolve --seen default when --lang is set and --seen not explicitly passed
    seen_arg: Path | None = args.seen
    if seen_arg is None and lang_suffix:
        # Derive from the configured seen_file: seen.txt → seen_es.txt, keeping
        # its own directory. Deriving from cfg_dir instead would silently point
        # at a different registry whenever seen_file lives in a subdirectory
        # (e.g. batch.seen_file: ./jobs/seen.txt), and every video would be
        # re-rendered.
        seen_arg = settings.seen_file.with_name(
            f"{settings.seen_file.stem}{lang_suffix}{settings.seen_file.suffix}"
        )

    # Apply lang suffix to output_dir
    if lang_suffix:
        settings.output_dir = (
            settings.output_dir.parent / f"{settings.output_dir.name}{lang_suffix}"
        )

    # Resolve --seen path relative to config.yaml's location (same as seen_file)
    seen_override: Path | None = None
    if seen_arg is not None:
        seen_override = seen_arg if seen_arg.is_absolute() else cfg_dir / seen_arg

    if args.status:
        seen_path = seen_override or settings.seen_file
        entries = seen.list_all(seen_path)
        print(f"\nSeen registry: {seen_path}")
        print(f"Total entries: {len(entries)}\n")
        for name in entries:
            print(f"  {name}")
        return 0

    if args.list_voices is not None:
        list_voices(settings, args.list_voices)
        return 0

    if args.list_bgm is not None:
        list_bgm_cmd(settings, args.list_bgm)
        return 0

    if args.upload_bgm is not None:
        upload_bgm_cmd(settings, Path(args.upload_bgm))
        return 0

    if not jobs_path.exists():
        print(f"[ERROR] Jobs file not found: {jobs_path}")
        print(f"        Copy jobs/jobs.example.yaml to {jobs_path.name} and add your video topics.")
        return 1

    return run(jobs_path, settings, dry_run=args.dry_run, seen_override=seen_override)


def main() -> None:
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
