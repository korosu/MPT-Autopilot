"""
reader.py — resolves the video topic and optional language hint from a given mp4 path.

Priority:
  topic   → script.json params.video_subject  >  filename stem
  lang    → caller-supplied --lang flag        >  script.json params.video_language
            >  None (LLM detects)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Same cap as enricher/llm.py::_MAX_TOPIC_LEN — the value ends up in an LLM prompt.
_MAX_TOPIC_LEN = 300


def _is_printable(ch: str) -> bool:
    """
    str.isprintable() is too permissive for our purposes: it returns True for
    VT (\\x0b), FF (\\x0c) and CR (\\x0d), all of which are control characters
    that must not reach an LLM prompt.
    """
    return ch.isprintable() and not ch.isspace()


def _sanitise_subject(raw: object) -> str:
    """
    Make a script.json video_subject safe to put into an LLM prompt.

    script.json is written by the batch stage but sits on disk as a plain file a
    user can hand-edit, so video_subject is untrusted here just like the
    filename. Stripping non-printables mirrors enricher/llm.py::_sanitise_topic
    and closes the asymmetry where the filename path was filtered but the
    script.json path was not.
    """
    cleaned = "".join(ch for ch in str(raw) if _is_printable(ch))
    return cleaned[:_MAX_TOPIC_LEN].strip()


@dataclass
class VideoMeta:
    topic: str
    language_hint: str | None  # None means "auto-detect via LLM"
    source: str  # "script_json" | "filename"
    mp4_path: Path
    json_path: Path  # path where output will be written (may not exist yet)


_LANG_CODE_MAP: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "ru": "Russian",
    "de": "German",
    "fr": "French",
    "pt": "Portuguese",
    "it": "Italian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "hi": "Hindi",
    "tr": "Turkish",
    "pl": "Polish",
    "nl": "Dutch",
}


def _normalize_lang(raw: str | None) -> str | None:
    """Convert short lang codes (en, es, ru) to full names (English, Spanish, Russian)."""
    if raw is None:
        return None
    raw = raw.strip()
    return _LANG_CODE_MAP.get(raw.lower(), raw)  # if unknown code → return as-is


def _stem_to_topic(stem: str) -> str:
    """Convert a filename stem to a human-readable topic string."""
    topic = stem.replace("_", " ").replace("-", " ")
    topic = re.sub(r"\s+", " ", topic).strip()
    return topic


def _read_script_json(json_path: Path) -> dict:
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_meta(mp4_path: Path, lang_override: str | None = None) -> VideoMeta:
    """
    Given the path to an mp4 file, return a VideoMeta with resolved topic and language.

    Args:
        mp4_path:       Path to the .mp4 file.
        lang_override:  Language name passed via --lang flag (takes top priority).
    """
    stem = mp4_path.stem
    json_path = mp4_path.with_suffix(".json")

    topic: str
    lang_hint: str | None
    source: str

    # --- Try script.json first ---
    if json_path.exists():
        data = _read_script_json(json_path)
        params = data.get("params", {}) if isinstance(data, dict) else {}
        if not isinstance(params, dict):
            params = {}
        video_subject = _sanitise_subject(params.get("video_subject", ""))
        script_lang = _normalize_lang(params.get("video_language"))

        if video_subject:
            topic = video_subject
            source = "script_json"
        else:
            topic = _stem_to_topic(stem)
            source = "filename"
    else:
        topic = _stem_to_topic(stem)
        source = "filename"
        script_lang = None

    # --- Resolve language priority ---
    if lang_override:
        lang_hint = _normalize_lang(lang_override)
    elif script_lang:
        lang_hint = script_lang
    else:
        lang_hint = None  # will be detected by LLM

    return VideoMeta(
        topic=topic,
        language_hint=lang_hint,
        source=source,
        mp4_path=mp4_path,
        json_path=json_path,
    )
