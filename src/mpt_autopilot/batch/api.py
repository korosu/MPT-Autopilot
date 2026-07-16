"""
engine/api.py

All communication with the MoneyPrinterTurbo REST API.

Three public functions:
  health_check(settings)              → bool
  submit_job(payload, settings)       → str   (task_id)
  wait_for_task(task_id, settings, log) → dict  (full task data on success)
"""

from __future__ import annotations

import time
from collections.abc import Callable

import requests

from mpt_batch.engine.settings import Settings


def health_check(settings: Settings) -> bool:
    """Quick GET to verify the MPT API is reachable. Returns True if healthy."""
    try:
        r = requests.get(f"{settings.api_url}/api/v1/tasks", timeout=10)
        r.raise_for_status()
        return True
    except Exception:
        return False


def submit_job(payload: dict, settings: Settings) -> str:
    """Submit a video generation job. Returns the task_id."""
    r = requests.post(f"{settings.api_url}/api/v1/videos", json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["data"]["task_id"]


def wait_for_task(
    task_id: str,
    settings: Settings,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Poll until the task completes (progress == 100) or fails.
    Returns the full task data dict on success.
    Raises RuntimeError / TimeoutError on failure, stall, or broken progress.
    """
    start = time.time()
    last_progress = -1
    last_progress_time = time.time()
    max_progress_seen = 0

    while True:
        if time.time() - start > settings.max_wait_seconds:
            raise TimeoutError(f"task timed out after {settings.max_wait_seconds}s")

        try:
            r = requests.get(f"{settings.api_url}/api/v1/tasks/{task_id}", timeout=30)
            r.raise_for_status()
            data = r.json()["data"]
        except Exception as exc:
            raise RuntimeError(f"API error while polling task: {exc}") from exc

        progress: int = data.get("progress", 0)
        state: int = data.get("state", 0)
        log(f"  [{task_id}] progress={progress}%")

        # Detect a broken task: progress reset to 0 after it had advanced
        if max_progress_seen > 0 and progress == 0:
            raise RuntimeError(
                f"progress dropped from {max_progress_seen}% to 0% — task likely broken"
            )
        if progress > max_progress_seen:
            max_progress_seen = progress

        # MPT states: -1=FAILED, 1=COMPLETE, 4=PROCESSING. Only -1 is terminal failure.
        # State 1 is alive until progress hits 100 (checked below).
        if state == -1:
            raise RuntimeError(f"task failed (state={state})")

        if progress >= 100:
            return data

        # Detect a stalled task
        if progress != last_progress:
            last_progress = progress
            last_progress_time = time.time()
        elif time.time() - last_progress_time > settings.stuck_threshold_seconds:
            raise RuntimeError(
                f"progress stuck at {progress}% for over {settings.stuck_threshold_seconds}s"
            )

        time.sleep(10)
