"""
batch/api.py

All communication with the MoneyPrinterTurbo REST API.

Three public functions:
  health_check(settings)              → bool
  submit_job(payload, settings)       → str   (task_id)
  wait_for_task(task_id, settings, log) → dict  (full task data on success)
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import httpx

from mpt_autopilot.batch.settings import Settings

_logger = logging.getLogger(__name__)

# Retry settings for transient polling errors (connection refused, timeout).
# Without these, a 30-second API blip would kill the task, force cleanup_task,
# and re-submit the entire job from scratch.
_POLL_MAX_RETRIES = 3
_POLL_RETRY_BACKOFF_BASE = 5.0  # seconds; doubles each attempt

# A task_id is only ever used as a single path segment under mpt_storage/tasks/.
# It comes from an unauthenticated, user-configurable HTTP endpoint, so it must
# be constrained here: an unvalidated id like ".." makes
# `mpt_storage / "tasks" / task_id` resolve to mpt_storage itself, and
# cleanup_task() would shutil.rmtree() the entire storage tree.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_task_id(task_id: object) -> str:
    """
    Return task_id if it is a safe single path segment, else raise ValueError.

    task_id is untrusted input from the MPT API response (submit_job) or from
    the in_progress.txt resume file, and it is interpolated into filesystem
    paths in run.py. Anything containing a path separator, a parent reference,
    or control characters is rejected rather than silently contained.
    """
    if not isinstance(task_id, str):
        raise ValueError(f"task_id must be a string, got {type(task_id).__name__}: {task_id!r}")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(
            f"refusing to use unsafe task_id {task_id!r}: a task_id must match "
            f"{_TASK_ID_RE.pattern} — it is used as a single path segment under "
            f"mpt_storage/tasks/. The API or in_progress.txt returned a value "
            f"that could escape that directory (path traversal)."
        )
    return task_id


def health_check(settings: Settings) -> bool:
    """
    Quick GET to verify the MPT API is reachable and answering sanely.

    A 200 is not enough: /api/v1/tasks is not a health endpoint, so a 200 with
    an error-shaped body (an outage surfaced as data, a gateway error page)
    would be treated as healthy and the batch would then fail job by job. The
    body must look like the expected {"data": [...]} task list.
    """
    try:
        r = httpx.get(f"{settings.api_url}/api/v1/tasks", timeout=10)
        r.raise_for_status()
        body = r.json()
        return isinstance(body, dict) and isinstance(body.get("data"), list)
    except (httpx.HTTPError, ValueError):
        return False


def submit_job(payload: dict, settings: Settings) -> str:
    """Submit a video generation job. Returns the task_id."""
    try:
        r = httpx.post(f"{settings.api_url}/api/v1/videos", json=payload, timeout=60)
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"API rejected job submission (HTTP {exc.response.status_code}): {exc}"
        ) from exc
    try:
        task_id = r.json()["data"]["task_id"]
    except (KeyError, TypeError, ValueError) as exc:
        # A 200 with an error-shaped body used to leak a bare KeyError('data')
        # with no context. Keep the body in the local log, not the message.
        body_preview = str(r.text)[:300].replace("\n", " ")
        _logger.warning("submit_job: unexpected response body: %s", body_preview)
        raise RuntimeError(
            "API returned a 200 for job submission but no task_id "
            "(missing 'data.task_id' in the response body)"
        ) from exc
    return validate_task_id(task_id)


def _as_int(value: object, field: str) -> int:
    """
    Coerce an API-supplied number to int, raising a clear error instead of
    letting a TypeError leak out of the poll loop.

    The annotations `progress: int = data.get("progress", 0)` are not checks.
    If the API returns {"progress": "50"} the comparison below raises
    TypeError, which is not in the except clauses around the request — so the
    whole batch run dies instead of failing the task. Same for a string
    "failed" in state: `state == -1` is False, so the loop polls until the
    2400s default timeout instead of failing fast.
    """
    if isinstance(value, bool):
        raise ValueError(f"API returned {field}={value!r}, expected a number")
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (ValueError, TypeError):
            raise ValueError(f"API returned non-numeric {field}={value!r}") from None
    raise ValueError(f"API returned {field}={value!r} ({type(value).__name__}), expected a number")


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

        # Retry transient polling errors (connection refused, read timeout) so a
        # brief API blip doesn't destroy an in-progress task.  HTTPStatusError
        # (4xx/5xx) is not retried — it means the server answered and the task
        # state is authoritative.
        data: dict | None = None
        for poll_attempt in range(_POLL_MAX_RETRIES + 1):
            try:
                r = httpx.get(f"{settings.api_url}/api/v1/tasks/{task_id}", timeout=30)
                r.raise_for_status()
                data = r.json()["data"]
                break  # success
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if poll_attempt >= _POLL_MAX_RETRIES:
                    raise RuntimeError(
                        f"API error while polling task after "
                        f"{_POLL_MAX_RETRIES + 1} attempts: {exc}"
                    ) from exc
                delay = _POLL_RETRY_BACKOFF_BASE * (2**poll_attempt)
                log(
                    f"  [{task_id}] polling error ({exc.__class__.__name__}), "
                    f"retrying in {delay:.0f}s"
                )
                time.sleep(delay)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"API error while polling task: {exc}") from exc

        assert data is not None, "polling loop exhausted without data"
        progress = _as_int(data.get("progress", 0), "progress")
        state = _as_int(data.get("state", 0), "state")
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
