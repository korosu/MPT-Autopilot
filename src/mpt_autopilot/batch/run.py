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

Exit codes:
    0 - every enabled job succeeded, or there was nothing to do
    1 - every attempted job failed, or the run aborted before starting
    2 - partial: some jobs succeeded and some failed. `mpt run` keeps going to
        enrich/upload on a 2 but counts it as a failure in its own exit code —
        2 at the pipeline level is reserved for an upload quota stop.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from mpt_autopilot import config as shared_config
from mpt_autopilot import notify, seen
from mpt_autopilot.batch import bgm, postprocess, state, voices
from mpt_autopilot.batch.api import (
    PayloadRejected,
    health_check,
    submit_job,
    validate_task_id,
    wait_for_task,
)
from mpt_autopilot.batch.settings import Settings
from mpt_autopilot.batch.settings import load as load_settings
from mpt_autopilot.batch.upload_post import (
    UploadPostError,
    UploadPostUnavailable,
    upload_video,
)
from mpt_autopilot.config import ConfigError
from mpt_autopilot.lock import file_lock, lock_path_for
from mpt_autopilot.logger import Logger, configure_root
from mpt_autopilot.uploader.metadata import title_from_filename

_log = logging.getLogger(__name__)

# Stale threshold for the batch run lock: a run can legitimately take hours
# (max_wait_seconds defaults to 2400s per task), so the keepalive in run()
# refreshes the lock's mtime; this is only the fallback for a dead holder.
_LOCK_STALE_AFTER = 1800

# ── Logging ───────────────────────────────────────────────────────────────────


def log(msg: str, settings: Settings, *, to_file: bool = True) -> None:
    """
    One line of batch output: timestamped, to stdout and (unless to_file=False)
    to the stage log file.

    Routes through the shared Logger instead of re-implementing rotation:
    RotatingFileHandler keeps three backups where the hand-rolled version kept
    one, and the level prefix now appears on stdout too (see logger.py). The
    Logger itself handles reconfiguration when mpt run points the stage at a
    different log file.

    to_file=False is the dry-run mode: preview only, no log file.
    """
    if not to_file:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
        return
    Logger(settings.log_file, settings.log_max_bytes, name="mpt_autopilot.batch").info(msg)


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


def _quality_warnings(dest_script: Path, copied_subtitles: int) -> list[str]:
    """
    Degrade-but-don't-fail conditions for a finished video (R3).

    Only checks observable facts: whether the copied script.json says
    subtitles were ordered (params.subtitle_enabled) and whether any
    .srt/.ass actually arrived. MPT's task-status response in the current
    checkout exposes no `warnings[]` field (TaskStatusData cuts it off), so
    the subtitle file is the only silent-failure marker we can see from here.
    """
    warnings: list[str] = []
    if copied_subtitles == 0 and dest_script.exists():
        try:
            script = json.loads(dest_script.read_text(encoding="utf-8"))
            params = script.get("params", {}) if isinstance(script, dict) else {}
            if isinstance(params, dict) and params.get("subtitle_enabled"):
                warnings.append(
                    "no subtitles: params.subtitle_enabled is true but no "
                    ".srt/.ass was produced by the render"
                )
        except (json.JSONDecodeError, OSError):
            # An unreadable script must not turn a successful copy into noise.
            pass
    return warnings


def _write_quality_warnings(dest_script: Path, quality_warnings: list[str]) -> None:
    """
    Persist the warnings into the sidecar so enrich/upload see the defect
    before publishing. Best-effort by design (R3): a warning that cannot be
    written must not fail an otherwise successful copy.
    """
    try:
        data = json.loads(dest_script.read_text(encoding="utf-8")) if dest_script.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError):
        return
    data["quality_warnings"] = quality_warnings
    try:
        dest_script.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


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
    copied_subtitles = 0
    for pattern in ["*.srt", "*.ass"]:
        for sub_file in task_dir.glob(pattern):
            dest_sub = (settings.output_dir / sub_file.name).resolve()
            if not dest_sub.parent == settings.output_dir.resolve():
                raise ValueError(f"refusing to copy subtitle outside output_dir: {sub_file}")
            shutil.copy2(sub_file, dest_sub)
            log(f"  saved: {dest_sub}", settings)
            copied_subtitles += 1

    # ── Quality check (R3 / B-12) ───────────────────────────────────────────
    # A finished render may still be degraded. The visible-to-us case: MPT
    # can silently produce no subtitle file (its Edge timing pass has a
    # deliberate no-Whisper-fallback), and a video without subtitles was
    # indistinguishable from a good one in logs and alerts.
    quality_warnings = _quality_warnings(dest_script, copied_subtitles)
    if quality_warnings:
        for warning in quality_warnings:
            log(f"  [quality] {output_file}: {warning}", settings)
        notify.alert(
            f"⚠️ Quality warnings for '{output_file}':\n" + "\n".join(quality_warnings),
            settings,
        )
        _write_quality_warnings(dest_script, quality_warnings)

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


def build_payload(job: dict, defaults: dict, voice_pool: dict) -> dict:
    """
    Merge a job onto the stage defaults exactly as the API will receive it.

    Single source of truth for both `--dry-run` preview and run_job: they used
    to strip different meta keys (preview only dropped "name"), so the preview
    could report a job as runnable while the real run sent a different payload.
    """
    meta_keys = ("name", "enabled", "output_file", "max_retries", "retry_delay_seconds")
    payload = {
        **defaults,
        **{k: v for k, v in job.items() if k not in meta_keys},
    }
    payload = voices.resolve(payload, voice_pool)
    warn_api_bounds(job, payload)
    return payload


# TaskVideoRequest caps MPT 1.3.7+ enforces on /api/v1/videos. The HTTP layer
# rejects out-of-range values with 400 — PayloadRejected — which batch does not
# retry (the payload never changes between attempts), so a stale value burned
# every retry, one per max_retries, and killed the run. Warn at payload-build
# time instead: still the job's own fetch-and-fail, but named before submission.
_API_BOUNDS = {
    "video_count": (1, 5),
    "video_clip_duration": (1, 15),
    "n_threads": (1, 16),
}


def warn_api_bounds(job: dict, payload: dict) -> None:
    """Warn when the payload carries values MPT's API request schema rejects."""
    name = job.get("name", job.get("output_file", "?"))
    for key, (lo, hi) in _API_BOUNDS.items():
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not lo <= value <= hi:
            _log.warning(
                "job %s: %s=%r is outside MPT's API bound %d..%d — the server "
                "will reject it with HTTP 400 (PayloadRejected, no retry)",
                name,
                key,
                value,
                lo,
                hi,
            )


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
    payload = build_payload(job, defaults, voice_pool)

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

            # ── Optional audio post-pass (R5) ─────────────────────────────────
            # Before upload-post and any cross-post: they must carry the
            # leveled audio, not the raw render.
            if settings.audio_normalize:
                postprocess.normalize_audio(dest_video, lambda m: log(m, settings))

            # ── upload-post (optional) ────────────────────────────────────────
            if settings.upload_post_enabled:
                try:
                    sidecar = dest_video.with_suffix(".json")
                    # B-9: batch runs before enrich, so the sidecar is usually
                    # raw script.json with no title/tags yet. Derive the title
                    # the same way the uploader stage would (underscores to
                    # spaces) instead of posting the bare filename slug.
                    meta_title: str
                    meta_desc = ""
                    meta_tags: list[str] = []
                    if sidecar.exists():
                        raw = json.loads(sidecar.read_text(encoding="utf-8"))
                        meta_title = str(raw.get("title") or title_from_filename(dest_video))
                        meta_desc = str(raw.get("description", ""))
                        raw_tags = raw.get("tags")
                        meta_tags = raw_tags if isinstance(raw_tags, list) else []
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

        except PayloadRejected as exc:
            # R6: a 4xx means the payload itself is wrong (a schema drift, an
            # invalid value). Retrying the identical bytes is pure waste —
            # fail the job once and name the server's own error detail.
            log(f"REJECTED (not retrying — fix the job or config): {exc}", settings)
            if task_id:
                cleanup_task(task_id, settings)
            state.remove(in_progress_path, job["output_file"])
            return False

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


def _seen_base_for_settings(config_path: Path | None) -> Path:
    # path_value() already falls back to the shared `paths:` block and then to
    # the default, so there is nothing to chain here. The previous
    # `... or cfg.path_value("paths", "seen_dir") or Path(".")` raised
    # ValueError("unknown config section 'paths'") whenever seen_dir was not
    # configured at all — "paths" is not a stage section.
    return shared_config.load(config_path).require_path("batch", "seen_dir", ".")


def _seen_path_for_settings(config_path: Path | None, lang_suffix: str = "") -> Path:
    return seen.resolve(_seen_base_for_settings(config_path), lang_suffix)


def run(
    jobs_path: Path,
    settings: Settings,
    *,
    dry_run: bool,
    config_path: Path | None = None,
    seen_override: Path | None = None,
    limit: int | None = None,
) -> int:
    if limit is not None and limit < 1:
        # argparse's type=int only proves it is a whole number; 0 and negatives
        # would silently make the run a no-op with a "0 videos" summary.
        log(f"ERROR: --limit must be 1 or greater, got {limit}", settings)
        return 1

    with open(jobs_path, encoding="utf-8") as f:
        jobs_cfg = yaml.safe_load(f) or {}

    defaults: dict = jobs_cfg.get("defaults", {})
    jobs: list[dict] = jobs_cfg.get("jobs", [])
    seen_file = seen_override or _seen_path_for_settings(config_path)
    already_seen = seen.load(seen_file)
    voice_pool = settings.voice_pool
    in_progress_path = seen_file.with_name(seen_file.stem + ".in_progress.txt")
    # Declared here (not inside `if pending:`) so the main loop's --limit check
    # can read it on runs with nothing to resume.
    resumed_produced = 0

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
        # --limit counts videos *produced*, and a resumed task produces one —
        # the cap belongs here too, or the main loop would double the budget.
        for entry in pending:
            if limit is not None and resumed_produced >= limit:
                log(
                    f"limit: {limit} video(s) already produced by resume — "
                    f"{len(pending) - resumed_produced} task(s) left for the next run",
                    settings,
                )
                break
            # Named `pending_file` (not `output_file`) so it cannot collide
            # with the main loop's own variable of that name — pyright treats
            # the two same-named declarations as one and mixes their types.
            pending_file: str = entry["output_file"]
            task_id: str = entry["task_id"]
            if pending_file in already_seen:
                state.remove(in_progress_path, pending_file)
                continue
            job = next((j for j in jobs if j.get("output_file") == pending_file), None)
            if not job:
                log(f"orphan in-progress entry '{pending_file}' (not in jobs file)", settings)
                state.remove(in_progress_path, pending_file)
                continue
            name = job.get("name", pending_file)
            log(f"resuming: {name} (task_id={task_id})", settings)
            try:
                task_data = wait_for_task(task_id, settings, lambda m: log(m, settings))
                _, resumed_video = copy_result(task_data, pending_file, settings)
                cleanup_task(task_id, settings)
                # R5: the resumed copy goes through the same audio pass as a
                # fresh one, so a crash mid-batch cannot leak an unnormalized
                # video into the exports.
                if settings.audio_normalize:
                    postprocess.normalize_audio(resumed_video, lambda m: log(m, settings))
                seen.add(seen_file, pending_file)
                state.remove(in_progress_path, pending_file)
                log(f"resumed and done: {pending_file}", settings)
                resumed_produced += 1
            except Exception as exc:
                log(
                    f"resume failed for '{pending_file}' (task_id={task_id}): {exc} — "
                    f"will re-submit as new task",
                    settings,
                )
                state.remove(in_progress_path, pending_file)
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
        # A preview that ignored --limit would describe work the real run
        # refuses to do — the same contract as the uploader's --limit.
        remaining = limit
        for job in jobs:
            name = job.get("name", job.get("output_file", "?"))
            if not job.get("enabled", True):
                print(f"  disabled  {name}")
                continue
            if not job.get("output_file"):
                print(f"  error     {name} - no output_file")
                continue
            if job["output_file"] in already_seen:
                print(f"  done      {name} (already done)")
                continue
            if remaining is not None and remaining <= 0:
                print(f"  skipped   {name} (--limit reached)")
                continue
            try:
                build_payload(job, defaults, voice_pool)
                print(f"  run       {name}")
                if remaining is not None:
                    remaining -= 1
            except KeyError as exc:
                print(f"  error     {name} - {exc}")
        if limit is not None and remaining is not None and remaining > 0:
            print(f"\n  (--limit {limit}: only {limit - remaining} job(s) available)")
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

    # The batch lock keys off a `<seen stem>.run` marker (file
    # `<seen stem>.run.lock`), NOT off seen_file itself: seen.add() takes
    # file_lock(seen_file) inside the loop and file_lock is not re-entrant —
    # the same target here would stall every registry write for the full wait
    # timeout (tests/batch/test_lock_collision.py guards exactly this).
    # A short acquisition wait lets a just-finished run release cleanly; the
    # strict mode turns "still locked" into a clean exit 1 rather than the
    # old silent 30-minute stall of a second batch.
    batch_lock_target = seen_file.with_name(seen_file.stem + ".run")
    # Counters live before the try, not inside the lock: a Ctrl+C that lands
    # while the lock is still being acquired (or during the "batch started"
    # alert) would otherwise reach the KeyboardInterrupt handler below with
    # these names unbound.
    ok: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    try:
        with file_lock(
            batch_lock_target,
            timeout=5.0,
            strict=True,
            stale_after=_LOCK_STALE_AFTER,
        ):
            settings.output_dir.mkdir(parents=True, exist_ok=True)
            start_time = time.time()
            notify.alert(
                f"Batch started: {jobs_path.name}\n"
                f"Total jobs: {len(jobs)}  To run: {to_run_count}  "
                f"Already done: {already_done_count}  Disabled: {disabled_count}"
                + (f"  Limit: {limit}" if limit is not None else ""),
                settings,
            )

            consecutive_failures = 0
            last_cleanup_at = 0

            for index, job in enumerate(jobs, start=1):
                name = job.get("name", job.get("output_file", "?"))

                # --limit counts videos produced (resume included — see the
                # resume loop). Checked *before* submitting, not after: a
                # post-success check lets one job past the cap submit, and
                # only the *next* one discovers the budget is gone.
                if limit is not None and len(ok) + resumed_produced >= limit:
                    log(
                        f"limit: {limit} video(s) generated — "
                        f"{len(jobs) - index + 1} job(s) left for the next run",
                        settings,
                    )
                    for remaining_job in jobs[index - 1 :]:
                        remaining_name = remaining_job.get(
                            "name", remaining_job.get("output_file", "?")
                        )
                        skipped.append(f"{remaining_name} (not attempted — --limit reached)")
                    break

                if not job.get("enabled", True):
                    skipped.append(f"{name} (disabled)")
                    continue

                # Annotated: pyright keeps the isinstance-narrowed type of the
                # previous loop iteration as the declared type and would reject
                # re-assigning `Unknown | None` to it on the next pass.
                output_file: object = job.get("output_file")
                if not isinstance(output_file, str) or not output_file.strip():
                    # A hand-edited jobs.yaml can miss the key. run_job is
                    # guarded by a wide except, but this check runs before it;
                    # without it a missing key killed the whole run with a
                    # traceback.
                    log(f"ERROR: job '{name}' has no output_file — skipped", settings)
                    failed.append(name)
                    continue

                if seen.contains(seen_file, output_file):
                    skipped.append(f"{name} (already done)")
                    log(f"skip: {output_file} (in seen registry)", settings)
                    continue

                success = run_job(job, defaults, voice_pool, settings, in_progress_path)

                if success:
                    ok.append(name)
                    consecutive_failures = 0
                    seen.add(seen_file, output_file)
                    # ponytail: keepalive — refresh the lock's mtime so a
                    # concurrent run never steals the lock of a long, alive
                    # batch (stale_after would otherwise misread it).
                    os.utime(lock_path_for(batch_lock_target), None)
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
    except TimeoutError as exc:
        log(f"ERROR: {exc}", settings)
        return 1
    except KeyboardInterrupt:
        # Ctrl+C here, or web Cancel's SIGINT (web/runner.py:interrupt_group):
        # the interrupt unwound the `with file_lock(...)` above, so the run
        # marker is already released. Only a hard kill (SIGTERM / Windows
        # taskkill) leaves the marker behind — stale_after or the web lock
        # sweep clear that. in_progress.txt keeps the in-flight task_ids, so
        # the next batch re-attaches to the running MPT tasks instead of
        # submitting duplicates; finished jobs are already in seen and stay.
        log(
            f"cancelled: {len(ok)} done, {len(failed)} failed, "
            f"{len(skipped)} skipped — unfinished tasks stay in "
            f"{in_progress_path.name} and resume on the next run",
            settings,
        )
        return 1

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
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Generate at most N videos this run, then stop. Disabled jobs, "
            "already-seen files and failures do not count toward N; a video "
            "resumed from an interrupted run does. Default: no limit."
        ),
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
            "Override the seen registry path for this run "
            "(e.g. --seen seen_es.txt for a one-off language-specific check)."
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
    mpt batch --limit 3                  # generate at most 3 videos this run

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
    seen.set_rotation_max_bytes(settings.seen_max_mb * 1024 * 1024)

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
        seen_arg = _seen_path_for_settings(config_path, lang_suffix)

    # Apply lang suffix to output_dir
    if lang_suffix:
        settings.output_dir = (
            settings.output_dir.parent / f"{settings.output_dir.name}{lang_suffix}"
        )

    # Resolve --seen path relative to config.yaml's location
    seen_override: Path | None = None
    if seen_arg is not None:
        seen_override = seen_arg if seen_arg.is_absolute() else cfg_dir / seen_arg

    if args.status:
        seen_path = seen_override or _seen_path_for_settings(config_path, lang_suffix)
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

    return run(
        jobs_path,
        settings,
        dry_run=args.dry_run,
        config_path=config_path,
        seen_override=seen_override,
        limit=getattr(args, "limit", None),
    )


def main() -> None:
    # Give the root logger a handler before any stage runs (M-1).
    configure_root()
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
