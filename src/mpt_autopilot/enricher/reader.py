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

# The sanitised topic ends up in an LLM prompt, so it is capped here. This is
# the one definition; enricher/llm.py imports both of these rather than keeping
# its own copy — two copies drifted once already, and the reader's dropped every
# space from the subject.
MAX_TOPIC_LEN = 300

# Control characters str.isprintable() lets through. VT, FF and CR must not
# reach an LLM prompt; ordinary whitespace must, because a topic is several
# words.
_FORBIDDEN_CONTROLS = "\x0b\x0c\x0d"

# Cap on the narration text read from script.json and handed to the
# title/description prompt (A-4): a 60–90s script is ~150–250 words, so 4000
# chars is generous headroom without letting a corrupt megabyte-sized file
# reach the LLM call budget.
MAX_SCRIPT_CHARS = 4000


def is_printable(ch: str) -> bool:
    """True for characters that may appear in a prompt. Whitespace qualifies."""
    return ch.isprintable() and ch not in _FORBIDDEN_CONTROLS


def sanitise_topic(raw: object) -> str:
    """
    Make an arbitrary topic string safe to put into an LLM prompt: drop
    non-printables, cap the length, trim the edges.

    script.json is written by the batch stage but sits on disk as a plain file a
    user can hand-edit, so video_subject is untrusted here just like the
    filename.
    """
    cleaned = "".join(ch for ch in str(raw) if is_printable(ch))
    return cleaned[:MAX_TOPIC_LEN].strip()


@dataclass
class VideoMeta:
    topic: str
    # Full narration text from script.json, when present (A-4): the hook of a
    # good title lives in the first line of the script, not in the subject.
    script: str
    language_hint: str | None  # None means "auto-detect via LLM"
    source: str  # "script_json" | "filename"
    mp4_path: Path
    json_path: Path  # path where output will be written (may not exist yet)


# Closed, hand-maintained mapping of ISO codes to the language names the LLM
# prompt template expects. Intentionally NOT auto-populated (no locale lists,
# no pycountry): the enricher prompt is only ever filled with languages the
# pipeline itself can produce, so an unknown code is passed through as-is
# rather than guessed at.
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
    script_text = ""
    lang_hint: str | None
    source: str

    # --- Try script.json first ---
    if json_path.exists():
        data = _read_script_json(json_path)
        params = data.get("params", {}) if isinstance(data, dict) else {}
        if not isinstance(params, dict):
            params = {}
        video_subject = sanitise_topic(params.get("video_subject", ""))
        script_lang = _normalize_lang(params.get("video_language"))
        # MPT writes the full narration under "script" (task.py: _task
        # kwargs); it is the raw material for the title/description stage.
        raw_script = data.get("script") if isinstance(data, dict) else None
        if isinstance(raw_script, str):
            script_text = raw_script.strip()[:MAX_SCRIPT_CHARS]

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
        script=script_text,
        language_hint=lang_hint,
        source=source,
        mp4_path=mp4_path,
        json_path=json_path,
    )
