"""
uploader.py — subprocess wrapper for youtubeuploader with retry logic.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

_MAX_RETRIES = 3
_BASE_DELAY = 2

# only these are retried — they occur before youtubeuploader finalizes
# the upload session, so a retry cannot create a duplicate. Any other non-zero
# exit may be post-finalize and is treated as permanent (fail fast). Expand the
# set only when a real failure proves a string is pre-finalize-safe.
_TRANSIENT_MARKERS = (
    "503",  # backend / service unavailable
    "backendError",
    "context deadline exceeded",
    "deadline exceeded",
    "timeout",
    "temporary",
    "try again later",
)


class UploadLimitExceeded(Exception):
    """Raised when YouTube's daily upload quota has been hit for an account."""


class UploadFailed(Exception):
    """Raised for any other non-zero exit from youtubeuploader."""


def _is_transient(output: str) -> bool:
    low = output.lower()
    return any(marker.lower() in low for marker in _TRANSIENT_MARKERS)


def upload_video(
    binary: Path,
    video: Path,
    meta_file: Path,
    client_secrets: Path,
    token_file: Path,
    *,
    sleep=time.sleep,
    timeout: int = 600,
) -> str:
    """
    Shells out to youtubeuploader (https://github.com/porjo/youtubeuploader).
    Retries with exponential backoff ONLY on transient (pre-finalize) failures,
    so a retry can never re-upload a video YouTube already finalized. Any other
    non-zero exit is permanent and fails fast. Returns combined stdout+stderr on
    success. Raises UploadLimitExceeded or UploadFailed on non-zero exit.

    Args:
        binary: Path to youtubeuploader executable.
        video: Path to the MP4 video file.
        meta_file: Path to the JSON metadata file.
        client_secrets: Path to OAuth client secrets JSON.
        token_file: Path to OAuth token cache file.
        sleep: Sleep function for testing (default: time.sleep).
        timeout: Max seconds to wait for upload (default: 600).
    """
    cmd = [
        str(binary),
        "-quiet",
        "-filename",
        str(video),
        "-metaJSON",
        str(meta_file),
        "-secrets",
        str(client_secrets),
        "-cache",
        str(token_file),
    ]

    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            output = f"timeout after {timeout}s"
            if _is_transient(output):
                last_exc = UploadFailed(f"transient error: {output}")
                if attempt < _MAX_RETRIES:
                    delay = _BASE_DELAY * (2 ** (attempt - 1))
                    print(f"[retry {attempt}/{_MAX_RETRIES}] timeout, retrying in {delay}s...")
                    sleep(delay)
                    continue
            raise UploadFailed(output)
        except OSError as exc:
            raise UploadFailed(f"failed to execute {binary}: {exc}") from exc

        if result.returncode == 0:
            return (result.stdout + result.stderr).strip()

        output = (result.stdout + result.stderr).strip()
        if "uploadLimitExceeded" in output:
            raise UploadLimitExceeded(output)
        if not _is_transient(output):
            raise UploadFailed(output or f"youtubeuploader exited with code {result.returncode}")

        msg = output or f"youtubeuploader exited with code {result.returncode}"
        last_exc = UploadFailed(f"transient error: {msg}")
        if attempt < _MAX_RETRIES:
            delay = _BASE_DELAY * (2 ** (attempt - 1))
            print(f"[retry {attempt}/{_MAX_RETRIES}] transient error, retrying in {delay}s...")
            sleep(delay)

    assert last_exc is not None
    raise last_exc
