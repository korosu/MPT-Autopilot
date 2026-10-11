"""
uploader.py — subprocess wrapper for youtubeuploader with retry logic.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

_MAX_RETRIES = 3
_BASE_DELAY = 2

# Only markers that PROVABLY mean the failure happened before youtubeuploader
# finalized the upload session — a retry after finalize would create a
# duplicate video on the channel, which is strictly worse than one lost
# attempt. These are Google API error reasons, matched on word boundaries: the
# old list also had raw substrings "503" and "timeout", which fired on those
# digits/words in unrelated output — a filename, a byte count (M-48). A
# subprocess timeout proves nothing about finalize either, so it is treated
# as permanent, not transient (M-49).
_TRANSIENT_MARKERS = (
    "backendError",
    "serviceUnavailable",
    "try again later",
    "temporary",
)


class UploadLimitExceeded(Exception):
    """Raised when YouTube's daily upload quota has been hit for an account."""


class UploadFailed(Exception):
    """Raised for any other non-zero exit from youtubeuploader."""


def _is_transient(output: str) -> bool:
    """Word-boundary match — '503' inside a byte count must not read as transient."""
    low = output.lower()
    return any(re.search(rf"\b{re.escape(marker.lower())}\b", low) for marker in _TRANSIENT_MARKERS)


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
        except subprocess.TimeoutExpired as exc:
            # Being killed on timeout proves nothing about whether
            # youtubeuploader had already finalized the upload; retrying could
            # duplicate the video on the channel. Permanent — the old code
            # built "timeout after Ns" and then matched its own "timeout"
            # marker, so this branch was tautologically transient (M-49).
            raise UploadFailed(
                f"timed out after {timeout}s — not retried: a retry after "
                "finalize would duplicate the video on the channel"
            ) from exc
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

    # All attempts exhausted on transient errors. A real raise rather than an
    # assert on last_exc, which `python -O` would strip.
    raise (
        last_exc
        if last_exc is not None
        else UploadFailed("upload retry loop exited without a result")
    )
