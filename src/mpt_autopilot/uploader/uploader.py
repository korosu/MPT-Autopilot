from __future__ import annotations

import subprocess
from pathlib import Path


class UploadLimitExceeded(Exception):
    """Raised when YouTube's daily upload quota has been hit for an account."""


class UploadFailed(Exception):
    """Raised for any other non-zero exit from youtubeuploader."""


def upload_video(
    binary: Path,
    video: Path,
    meta_file: Path,
    client_secrets: Path,
    token_file: Path,
) -> str:
    """
    Shells out to youtubeuploader (https://github.com/porjo/youtubeuploader).
    Returns combined stdout+stderr on success. Raises UploadLimitExceeded or
    UploadFailed on non-zero exit.
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
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        # e.g. the binary was removed/became unexecutable mid-run, after the
        # settings.validate_account_ready() preflight check already passed.
        raise UploadFailed(f"failed to execute {binary}: {exc}") from exc

    output = (result.stdout + result.stderr).strip()

    if result.returncode != 0:
        if "uploadLimitExceeded" in output:
            raise UploadLimitExceeded(output)
        raise UploadFailed(output or f"youtubeuploader exited with code {result.returncode}")

    return output
