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

from mpt_autopilot._http_utils import get_shared_client
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


class PayloadRejected(Exception):
    """
    MPT answered 4xx to a submission (R6). A contract error is deterministic:
    retrying the same payload just burns retry_delay per attempt — B-1 spent
    3×180 s this way before dying on max_consecutive_failures. The batch
    stage treats this as "do not retry".
    """


def _auth_headers(settings: Settings) -> dict[str, str]:
    """
    x-api-key when MPT is configured to require one (R6): its base controller
    reads the header on every route, and without it a key-enabled MPT is
    unusable from Autopilot at all. The secret comes from .env (MPT_API_KEY).
    """
    api_key = getattr(settings, "api_key", "")
    if api_key:
        return {"x-api-key": api_key}
    return {}


def _error_detail(response: httpx.Response) -> str:
    """
    Best-effort extraction of MPT's structured error body (R6).

    MPT's validation handler answers 400 with the pydantic error list in
    `data` and a short `message` (app/asgi.py) — naming the field turns a
    bare HTTP code into a diagnosis (the exact B-1 experience, had this
    existed then).
    """
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:300].replace("\n", " ")
    if not isinstance(body, dict):
        return str(body)[:300]
    parts: list[str] = []
    if body.get("message"):
        parts.append(str(body["message"]))
    data = body.get("data")
    if isinstance(data, list):
        for item in data[:5]:
            if isinstance(item, dict):
                parts.append(f"{item.get('loc')}: {item.get('msg')}")
            else:
                parts.append(str(item))
    return "; ".join(parts) if parts else str(body)[:300]


def health_check(settings: Settings) -> bool:
    """
    Quick GET to verify the MPT API is reachable and answering sanely.

    A 200 is not enough: /api/v1/tasks is not a health endpoint, so a 200
    with an error-shaped body (an outage surfaced as data, a gateway error
    page) would be treated as healthy and the batch would then fail job by
    job. The body must include either a list at ``data`` or a task-list
    object at ``data.tasks``.
    """
    try:
        r = get_shared_client().get(
            f"{settings.api_url}/api/v1/tasks", headers=_auth_headers(settings), timeout=10
        )
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict):
            # A gateway error page or a bare JSON array at the top level is not
            # a task listing; body.get("data") on a list is an AttributeError
            # that this function's except clauses do not catch.
            return False
        data = body.get("data")
        # MPT wraps the list in {"data": {"tasks": [...], "total": N, ...}}.
        # Accept either shape so health_check doesn't reject a valid response.
        if isinstance(data, list):
            return True
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            return True
        return False
    except (httpx.HTTPError, ValueError):
        return False


def submit_job(payload: dict, settings: Settings) -> str:
    """Submit a video generation job. Returns the task_id."""
    try:
        r = get_shared_client().post(
            f"{settings.api_url}/api/v1/videos",
            json=payload,
            headers=_auth_headers(settings),
            timeout=60,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = _error_detail(exc.response)
        if 400 <= status < 500 and status != 429:
            # R6: a 4xx is our payload's fault, not a network blip — fail the
            # job immediately with the server's own field-level detail.
            raise PayloadRejected(f"MPT rejected the submission (HTTP {status}): {detail}") from exc
        # 5xx and 429 stay retryable: the caller's retry loop exists for
        # exactly this class of transient trouble.
        raise RuntimeError(f"API rejected job submission (HTTP {status}): {detail or exc}") from exc
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
    log: Callable[[str], None] | None = None,
) -> dict:
    """
    Poll until the task completes (progress == 100) or fails.
    Returns the full task data dict on success.
    Raises RuntimeError / TimeoutError on failure, stall, or broken progress.

    `log` is optional; the batch run always passes its own line logger, but
    callers that only want the result (tests, resume tooling) get silence
    rather than accidental stdout writes.
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
                r = get_shared_client().get(
                    f"{settings.api_url}/api/v1/tasks/{task_id}",
                    headers=_auth_headers(settings),
                    timeout=30,
                )
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
                if log is not None:
                    log(
                        f"  [{task_id}] polling error ({exc.__class__.__name__}), "
                        f"retrying in {delay:.0f}s"
                    )
                time.sleep(delay)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"API error while polling task: {exc}") from exc

        if data is None:
            # Unreachable: the inner loop either breaks with data or raises. A
            # raise rather than an assert, which `python -O` would strip.
            raise RuntimeError("polling loop exhausted without data")
        progress = _as_int(data.get("progress", 0), "progress")
        state = _as_int(data.get("state", 0), "state")
        if log is not None:
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
            # TaskStatusData carries failed_stage/error; naming them turns a
            # bare "task failed" into a diagnosis (R3).
            stage = data.get("failed_stage")
            error = data.get("error")
            detail = f", stage={stage}" if stage else ""
            detail += f", error={error}" if error else ""
            raise RuntimeError(f"task failed (state={state}{detail})")

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

        time.sleep(settings.poll_interval_seconds)
