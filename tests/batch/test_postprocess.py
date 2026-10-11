"""Tests for the optional audio post-pass (R5).

The pass levels loudness and trims the trailing silence in ONE ffmpeg call
with `-c:v copy` — the video stream must never be re-encoded. All failure
modes (no ffmpeg, non-zero exit, timeout) are non-fatal: the original file
always survives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from mpt_autopilot.batch import postprocess


def _video(tmp_path: Path) -> Path:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VID")
    return video


def test_missing_ffmpeg_skips_and_keeps_original(tmp_path: Path):
    video = _video(tmp_path)
    logs: list[str] = []
    with patch.object(postprocess, "ffmpeg_available", return_value=False):
        assert postprocess.normalize_audio(video, logs.append) is False
    assert video.read_bytes() == b"VID"
    assert any("ffmpeg not found" in line for line in logs)


def test_normalization_uses_c_v_copy_and_replaces_in_place(tmp_path: Path):
    video = _video(tmp_path)

    def fake_run(cmd, **kwargs):
        # Simulate ffmpeg writing the output file named in the command.
        Path(cmd[-1]).write_bytes(b"NORMALIZED")
        return MagicMock(returncode=0, stderr="")

    with (
        patch.object(postprocess, "ffmpeg_available", return_value=True),
        patch.object(postprocess.subprocess, "run", side_effect=fake_run) as run,
    ):
        assert postprocess.normalize_audio(video, lambda m: None) is True

    cmd = run.call_args.args[0]
    assert "-c:v" in cmd and "copy" in cmd
    joined = " ".join(cmd)
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in joined
    assert "silenceremove" in joined  # tail trim in the SAME filter chain
    assert video.read_bytes() == b"NORMALIZED"
    assert not list(tmp_path.glob("*.normalized*"))  # temp cleaned up


def test_ffmpeg_failure_keeps_original_and_reports(tmp_path: Path):
    video = _video(tmp_path)
    logs: list[str] = []

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"PARTIAL")
        return MagicMock(returncode=1, stderr="boom")

    with (
        patch.object(postprocess, "ffmpeg_available", return_value=True),
        patch.object(postprocess.subprocess, "run", side_effect=fake_run),
    ):
        assert postprocess.normalize_audio(video, logs.append) is False

    assert video.read_bytes() == b"VID"
    assert any("exited 1" in line for line in logs)
    assert not list(tmp_path.glob("*.normalized*"))


def test_ffmpeg_timeout_keeps_original(tmp_path: Path):
    video = _video(tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)

    with (
        patch.object(postprocess, "ffmpeg_available", return_value=True),
        patch.object(postprocess.subprocess, "run", side_effect=fake_run),
    ):
        assert postprocess.normalize_audio(video, lambda m: None) is False

    assert video.read_bytes() == b"VID"


def test_validate_warns_when_audio_normalize_without_ffmpeg(tmp_path: Path, monkeypatch):
    """R5: the promise is visible at validation time, not silently broken on
    every render."""
    from mpt_autopilot import validate

    monkeypatch.setattr("mpt_autopilot.validate.shutil.which", lambda name: None)
    cfg = validate.shared_config.load(_config(tmp_path, "audio_normalize: true"))

    results = validate._check_audio_normalize(cfg)

    assert len(results) == 1
    assert results[0].status == "warn"
    assert "ffmpeg" in results[0].detail


def test_validate_silent_when_audio_normalize_off(tmp_path: Path, monkeypatch):
    from mpt_autopilot import validate

    monkeypatch.setattr("mpt_autopilot.validate.shutil.which", lambda name: None)
    cfg = validate.shared_config.load(_config(tmp_path))

    assert validate._check_audio_normalize(cfg) == []


def _config(tmp_path: Path, batch_extra: str = "") -> Path:
    body = (
        "langs:\n"
        "  en:\n"
        "    label: English\n"
        '    file_suffix: ""\n'
        "batch:\n"
        "  api_url: http://127.0.0.1:9\n"
        "  mpt_storage: ./s\n"
        "  mpt_songs_dir: ./songs\n"
    )
    if batch_extra:
        body += f"  {batch_extra}\n"
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
