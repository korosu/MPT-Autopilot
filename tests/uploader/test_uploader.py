from __future__ import annotations

from pathlib import Path

import pytest

from yt_uploader.engine.uploader import UploadFailed, UploadLimitExceeded, upload_video


def _fake_binary(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "fake_uploader.sh"
    path.write_text(f"#!/bin/bash\n{script}\n")
    path.chmod(0o755)
    return path


def test_quota_exceeded_is_detected(tmp_path):
    binary = _fake_binary(tmp_path, "echo 'uploadLimitExceeded: quota' >&2\nexit 1")
    with pytest.raises(UploadLimitExceeded):
        upload_video(binary, Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))


def test_generic_failure_is_upload_failed(tmp_path):
    binary = _fake_binary(tmp_path, "echo 'boom' >&2\nexit 1")
    with pytest.raises(UploadFailed):
        upload_video(binary, Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))


def test_success_returns_output(tmp_path):
    binary = _fake_binary(tmp_path, "echo 'all good'\nexit 0")
    output = upload_video(binary, Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))
    assert "all good" in output


def test_missing_binary_raises_upload_failed_not_a_crash(tmp_path):
    # No preflight check at this layer - upload_video() itself must not let
    # a raw OSError escape when the binary doesn't exist / isn't executable.
    missing = tmp_path / "does-not-exist"
    with pytest.raises(UploadFailed):
        upload_video(missing, Path("v.mp4"), Path("m.json"), Path("s"), Path("t"))
