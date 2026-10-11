"""
batch/postprocess.py — optional audio post-pass on the finished video (R5).

MoneyPrinterTurbo has no loudness normalization, no ducking, and no trimming
of the ~0.9s silence tail Edge TTS leaves behind (verified: no loudnorm /
silenceremove anywhere in its render path). Loudness therefore jumps between
videos, which is the most audible "amateur" marker on a feed — and both are
fixable in ONE ffmpeg pass on our side, after the render and before the
upload/cross-post sees the file.

The pass is deliberately audio-only: `-c:v copy` leaves the video stream
untouched. MPT already runs the pixels through two or three lossy encodes;
a fourth would cost more than leveling the sound gains.

Enabled by `batch.audio_normalize: true` (default off — existing setups must
not silently change behaviour or gain an ffmpeg dependency).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

# R119 target for short-form feeds: -14 LUFS integrated, -1.5 dBTP ceiling,
# 11 LU range — the EBU R128 "podcast/shorts" triplet.
_FILTER = (
    "areverse,"
    "silenceremove=start_periods=1:start_silence=0.2:start_threshold=-50dB,"
    "areverse,"
    "loudnorm=I=-14:TP=-1.5:LRA=11"
)
_TIMEOUT_SECONDS = 300


def ffmpeg_available() -> bool:
    """True when an ffmpeg binary is on PATH (the pass is optional, so is the tool)."""
    return shutil.which("ffmpeg") is not None


def normalize_audio(video: Path, log: Callable[[str], None]) -> bool:
    """
    Level the loudness and trim the trailing silence of `video`, in place.

    Returns True when the file was rewritten, False otherwise (no ffmpeg, or
    ffmpeg failed — both are logged and NON-FATAL: a video with uneven audio
    still beats a missing video).

    The tail trim runs inside the same filter chain as loudnorm so the audio
    stream is re-encoded once, not twice (R5's own requirement).
    """
    if not ffmpeg_available():
        log(f"  [audio] ffmpeg not found on PATH — skipping normalization for {video.name}")
        return False

    tmp = video.with_name(video.stem + ".normalized" + video.suffix)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-af",
        _FILTER,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log(f"  [audio] ffmpeg timed out after {_TIMEOUT_SECONDS}s — keeping the original")
        tmp.unlink(missing_ok=True)
        return False
    except OSError as exc:
        log(f"  [audio] ffmpeg failed to start: {exc} — keeping the original")
        tmp.unlink(missing_ok=True)
        return False

    if result.returncode != 0 or not tmp.exists():
        preview = (result.stderr or "")[-300:].replace("\n", " ")
        log(f"  [audio] ffmpeg exited {result.returncode}: {preview} — keeping the original")
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(video)
    log(f"  [audio] normalized: {video.name} (loudnorm -14 LUFS, tail trimmed)")
    return True
