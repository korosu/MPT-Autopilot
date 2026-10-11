"""Tests for the post-copy quality check (R3 / B-12).

A finished render may still be degraded: MPT can silently produce no subtitle
file (its Edge timing pass deliberately has no Whisper fallback), and without
this check a subtitle-less video was indistinguishable from a good one in
logs, alerts, and the sidecar that enrich/upload read.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mpt_autopilot.batch import run as batch_run


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        mpt_storage=tmp_path / "storage",
        output_dir=tmp_path / "exports",
        log_file=tmp_path / "nop.log",
        log_max_bytes=10**7,
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="test",
    )


def _task_dir(settings: SimpleNamespace, task_id: str = "T1") -> Path:
    task_dir = settings.mpt_storage / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "final-1.mp4").write_bytes(b"VID")
    return task_dir


def _task_data(task_id: str = "T1") -> dict:
    return {"task_id": task_id, "videos": [], "combined_videos": []}


def _script_with_params(task_dir: Path, subtitle_enabled: bool) -> None:
    (task_dir / "script.json").write_text(
        json.dumps({"params": {"subtitle_enabled": subtitle_enabled}, "script": "text"}),
        encoding="utf-8",
    )


def test_missing_subtitles_are_reported_and_marked(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    _task_dir(settings)
    _script_with_params(settings.mpt_storage / "tasks" / "T1", subtitle_enabled=True)
    (settings.output_dir).mkdir()
    monkeypatch.setattr(batch_run, "log", lambda msg, s, to_file=True: None)
    monkeypatch.setattr(batch_run.notify, "alert", lambda msg, s: None)

    batch_run.copy_result(_task_data(), "video.mp4", settings)  # type: ignore[arg-type]

    sidecar = settings.output_dir / "video.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert any("no subtitles" in w for w in data["quality_warnings"])


def test_present_subtitles_produce_no_warning(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    task_dir = _task_dir(settings)
    _script_with_params(task_dir, subtitle_enabled=True)
    (task_dir / "final-1.srt").write_text("1\n00:00:00 --> ...\ntext", encoding="utf-8")
    settings.output_dir.mkdir()
    monkeypatch.setattr(batch_run, "log", lambda msg, s, to_file=True: None)
    monkeypatch.setattr(batch_run.notify, "alert", lambda msg, s: None)

    batch_run.copy_result(_task_data(), "video.mp4", settings)  # type: ignore[arg-type]

    sidecar = settings.output_dir / "video.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "quality_warnings" not in data


def test_disabled_subtitles_produce_no_warning(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    _task_dir(settings)
    _script_with_params(settings.mpt_storage / "tasks" / "T1", subtitle_enabled=False)
    settings.output_dir.mkdir()
    monkeypatch.setattr(batch_run, "log", lambda msg, s, to_file=True: None)
    monkeypatch.setattr(batch_run.notify, "alert", lambda msg, s: None)

    batch_run.copy_result(_task_data(), "video.mp4", settings)  # type: ignore[arg-type]

    sidecar = settings.output_dir / "video.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "quality_warnings" not in data


def test_failed_task_names_the_stage():
    from unittest.mock import MagicMock, patch

    from mpt_autopilot.batch.api import wait_for_task

    response = MagicMock(
        raise_for_status=MagicMock(),
        json=lambda: {
            "data": {
                "progress": 30,
                "state": -1,
                "failed_stage": "audio",
                "error": "TTS request timed out",
            }
        },
    )
    settings = MagicMock(
        api_url="http://x",
        max_wait_seconds=30,
        stuck_threshold_seconds=60,
        poll_interval_seconds=0,
    )
    with patch("mpt_autopilot.batch.api.get_shared_client") as gsc:
        client = MagicMock()
        gsc.return_value = client
        client.get.return_value = response
        try:
            wait_for_task("t", settings)
            raise AssertionError("should have raised")
        except RuntimeError as exc:
            assert "stage=audio" in str(exc)
            assert "TTS request timed out" in str(exc)


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
