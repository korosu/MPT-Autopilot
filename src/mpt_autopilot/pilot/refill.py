#!/usr/bin/env python3
"""
refill.py — pilot stage entry point (`mpt refill`).

Refills the queue by:
- (default) calling LLM to generate new video ideas when pending jobs drop below threshold,
- (--topic/--topics) importing topics verbatim as jobs without LLM,
- (--theme) calling LLM to generate short topic titles constrained to configured themes.

Where jobs/seen files live (priority order):
    1. --jobs-dir / --seen-dir (explicit CLI flags)
    2. paths.jobs_dir / paths.seen_dir in config.yaml
    3. current directory

Usage:
    # LLM mode (default):
    refill --lang en --jobs-dir /your/path/to/jobs
    refill --lang en --jobs-dir /your/path/to/jobs --force

    # Topics mode (no LLM, topic text = video_subject):
    refill --lang en --topic "The tongue is not the strongest muscle in your body"
    refill --lang en --topics /path/to/topics.txt

    # Theme mode (LLM, constrained themes):
    refill --lang en --force --count 5           # uses all themes from config.yaml
    refill --lang en --theme job --force --count 5  # only "job" theme
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import re
import sys
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mpt_autopilot import seen
from mpt_autopilot.logger import configure_root
from mpt_autopilot.notify import alert as notify_alert
from mpt_autopilot.pilot import jobs
from mpt_autopilot.pilot.llm import call_llm, parse_json_array
from mpt_autopilot.pilot.prompt import (
    THEME_MAX_SUBJECT_CHARS,
    VIDEO_SUBJECT_MAX_CHARS,
    build_themes,
)
from mpt_autopilot.pilot.prompt import build as build_prompt
from mpt_autopilot.pilot.settings import LangSettings, Settings
from mpt_autopilot.pilot.settings import load as load_settings
from mpt_autopilot.seen import load_ordered as seen_load_ordered

_log = logging.getLogger(__name__)

# ponytail: calibration for natural gemini TTS speed (~143 words/min).
# voice_rate does NOT affect duration for gemini voices (see MPT voice.py:1813).
# On voice change: move to config.yaml langs.<code>.words_per_second.
WORDS_PER_SECOND = 2.39


def _parse_positive_int(token: str, part: str, s: str) -> int:
    """
    Parse one duration_range token into a positive int, with a diagnostic that
    names the failing part and shows the original input.
    """
    try:
        value = int(token)
    except ValueError:
        raise ValueError(f"duration_range '{s}': {part} ({token!r}) is not an integer") from None
    if value <= 0:
        raise ValueError(f"duration_range '{s}' has non-positive {part} value")
    return value


def parse_duration_range(s: str | None) -> tuple[int, int | None] | None:
    """
    Parse duration_range string into (min_seconds, max_seconds_or_none).

    Format validation raises its own messages, and they are deliberately not
    swallowed: the outer except below only catches int() conversion failures.
    Wrapping the whole block in `except ValueError` used to replace
    "has min (60) greater than max (30)" with a generic "invalid: expected
    'MIN-MAX'", so a user could not tell which constraint they had broken.
    """
    if not s:
        return None
    s = s.strip()
    if s.endswith("+"):
        return (_parse_positive_int(s[:-1], "min value", s), None)
    if "-" in s:
        left, right = s.split("-", 1)
        lo = _parse_positive_int(left, "min value", s)
        hi = _parse_positive_int(right, "max value", s)
        if lo > hi:
            raise ValueError(f"duration_range '{s}' has min ({lo}) greater than max ({hi})")
        return (lo, hi)
    raise ValueError(f"duration_range '{s}' invalid: expected 'MIN-MAX' or 'MIN+' format")


def duration_to_words(sec: tuple[int, int | None]) -> tuple[int, int | None]:
    """Convert (min_seconds, max_seconds) to word bounds. ceil for min (guarantee), floor for max."""  # noqa: E501
    lo, hi = sec
    return (
        math.ceil(lo * WORDS_PER_SECOND),
        None if hi is None else math.floor(hi * WORDS_PER_SECOND),
    )


def paragraph_floor(sec: tuple[int, int | None]) -> int:
    """Return paragraph_number floor based on upper bound (longer script → more paragraphs)."""
    _, hi = sec
    if hi is None or hi > 90:
        return 3
    if hi > 60:
        return 2
    return 1


_HOOK_INSTRUCTION = (
    "Open with a single-sentence hook in the first 3–5 seconds of voiceover: "
    "a shocking claim, counterintuitive observation, teaser question, or vivid "
    "image. Do NOT start with greetings, 'did you know', filler, or throat-clearing."
)

# [pause:Xs] tags are deliberately NOT requested anymore (B-4): MPT's script
# post-processing strips bracketed tags before TTS even for Edge voices, so
# the instruction only burned the 2000-char video_script_prompt budget.


def build_duration_instruction(words: tuple[int, int | None] | None) -> str:
    """Build video_script_prompt: always-on hook + (optional) word-count guidance."""
    parts = [_HOOK_INSTRUCTION]
    if words is not None:
        lo, hi = words
        if hi is None:
            parts.append(
                f"Narrate at least {lo} words. Do not pad artificially; keep a natural "
                f"spoken pace. Do not mention word counts."
            )
        else:
            parts.append(
                f"Narrate between {lo} and {hi} words. Do not exceed {hi} words; "
                f"do not fall below {lo} words. Do not pad or cut artificially; "
                f"keep a natural spoken pace. Do not mention word counts."
            )
    return " ".join(parts)


# ── Deduplication + cleanup ───────────────────────────────────────────────────

# Known valid values for MPT fields (whitelist for LLM output validation).
#
# Both lists mirror MoneyPrinterTurbo's own handling, not a superset of it:
#   * VideoConcatMode is an enum with exactly two members. "reverse" and
#     "transition" are separate parameters there, not concat modes.
#   * get_bgm_file() honours "random" (pick from the songs dir) and treats any
#     other truthy value as "use the bgm_file I was given" — which is what
#     `mpt batch --list-bgm` suggests as bgm_type: "custom". "none" means no
#     background music.
_VALID_CONCAT_MODES = {"random", "sequential"}
_VALID_BGM_TYPES = {"random", "custom", "none"}

# Upstream VideoFitMode, VideoTransitionMode, SubtitleDisplayMode and
# SubtitleAnimation — mirrors MoneyPrinterTurbo app/models/schema.py.
_VALID_FIT_MODES = {"cover", "contain"}
# The transition enum has NO "None" member: its "none" case carries a Python
# None, which serialises to JSON null — and the schema field is
# Optional[VideoTransitionMode] = None. "No transition" therefore means the
# key is ABSENT (or null), never the string "None" (B-1: pydantic rejected the
# string with HTTP 400, which retried 3×180s and killed the batch).
_VALID_TRANSITION_MODES = {
    "Shuffle",
    "FadeIn",
    "FadeOut",
    "SlideIn",
    "SlideOut",
    "ZoomIn",
    "ZoomOut",
}
_VALID_DISPLAY_MODES = {"sentence", "word_by_word"}
_VALID_ANIMATIONS = {"none", "pop_spring"}
# ImageMotion (MPT 1.3.9): zoom = slow zoom on still/AI images (server
# default), static = no motion. New field — "a still image without transition
# effects" now selects Static + transition none.
_VALID_IMAGE_MOTIONS = {"static", "zoom"}


def _num_default(defaults: dict, key: str, fallback: float) -> float:
    """
    Read a numeric job_defaults value, ignoring a non-numeric one.

    The validations below fall back to `defaults.get(key, ...)` when the LLM
    sends garbage. A config.yaml holding `paragraph_number: "2"` made that
    fallback just as broken as the value it replaced — and `max()` against the
    paragraph_floor band then raised TypeError mid-run.
    """
    value = defaults.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return value


def _int_default(defaults: dict, key: str, fallback: int) -> int:
    """Positive-int variant of _num_default (paragraph_number feeds max())."""
    value = _num_default(defaults, key, fallback)
    return int(value) if value > 0 else fallback


def _validate_against_config(job: dict, lang_cfg: LangSettings) -> dict:
    """
    Check the fields the LLM was asked to fill in (voice_name, voice_rate,
    video_script, video_clip_duration, video_concat_mode, bgm_type,
    bgm_volume, video_fit_mode, video_transition_mode, subtitle_display_mode,
    subtitle_animation, rounded_subtitle_background, video_clip_speed,
    video_music_prompt, sonilo_bgm_prompt, custom_system_prompt,
    video_materials) against config.yaml and MPT schema constraints. Anything
    missing, the wrong type, out of range, or exceeding MPT length limits is
    replaced with a safe configured default instead of being written into the
    jobs yaml as-is.

    Always injects an always-on hook instruction into video_script_prompt.
    If duration_range is configured, also adds word-count guidance and may
    adjust paragraph_number upward per the band floor.
    """
    defaults = lang_cfg.job_defaults
    out = dict(job)

    # Voice assignment is mechanical, not prompt-begged: whatever the LLM
    # (or a hand-built payload) put in voice_name is overwritten from the
    # config pool by a stable hash of the job's name. crc32 instead of
    # hash() — the builtin is salted per process (PYTHONHASHSEED), which
    # would make the assignment change between runs. Same topic → same
    # voice forever; pool covered roughly uniformly. An empty pool is NOT
    # handled here (a job without a voice would silently fall back to MPT's
    # Edge default) — run()'s pre-flight rejects it before any LLM call.
    voices = lang_cfg.voices
    if voices:
        job_name = str(out.get("name") or out.get("output_file") or "")
        out["voice_name"] = voices[zlib.crc32(job_name.encode("utf-8")) % len(voices)]

    rate = out.get("voice_rate")
    if (
        not isinstance(rate, (int, float))
        or isinstance(rate, bool)
        or not (lang_cfg.voice_rate_min <= rate <= lang_cfg.voice_rate_max)
    ):
        out["voice_rate"] = lang_cfg.voice_rate_min

    clip = out.get("video_clip_duration")
    if not isinstance(clip, (int, float)) or isinstance(clip, bool) or clip <= 0:
        out["video_clip_duration"] = _num_default(defaults, "video_clip_duration", 3)

    volume = out.get("bgm_volume")
    if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not (0 <= volume <= 1):
        out["bgm_volume"] = _num_default(defaults, "bgm_volume", 0.15)

    para = out.get("paragraph_number")
    if not isinstance(para, int) or isinstance(para, bool) or para <= 0:
        out["paragraph_number"] = _int_default(defaults, "paragraph_number", 2)

    # video_concat_mode: must be a known MPT value
    if out.get("video_concat_mode") not in _VALID_CONCAT_MODES:
        out["video_concat_mode"] = defaults.get("video_concat_mode", "random")

    # bgm_type: must be a known MPT value
    if out.get("bgm_type") not in _VALID_BGM_TYPES:
        out["bgm_type"] = defaults.get("bgm_type", "random")

    # video_script: optional pre-written script — pass through as-is.
    # The pilot doesn't generate it, but a hand-edited or API-imported job
    # may already contain one; don't clobber it with the hook prompt.
    # Defensive check: drop obviously broken values (non-string or > 8k chars).
    vs_raw = out.get("video_script")
    if not isinstance(vs_raw, str) or len(vs_raw) > 8000:
        out["video_script"] = defaults.get("video_script", "")

    # --- New upstream fields (MPT v1.3.6 VideoParams additions) ---
    # video_fit_mode: how non-matching-aspect clips fill the canvas
    if out.get("video_fit_mode") not in _VALID_FIT_MODES:
        out["video_fit_mode"] = defaults.get("video_fit_mode", "cover")

    # video_transition_mode: clip-to-clip transition effect. "No transition"
    # is the absence of the key — the MPT enum has no "None" string member
    # (B-1), so an unrecognised value (including a legacy "None") removes the
    # key instead of substituting a string pydantic would reject.
    t_mode = out.get("video_transition_mode")
    if t_mode not in _VALID_TRANSITION_MODES:
        t_mode = defaults.get("video_transition_mode")
    if t_mode not in _VALID_TRANSITION_MODES:
        out.pop("video_transition_mode", None)
    else:
        out["video_transition_mode"] = t_mode

    # video_terms (B-7): explicit stock-search terms. When non-empty, MPT
    # skips its own English-only 5-term generator entirely — the only lever
    # over the visual row, which the pilot used to drop on write. Accept
    # str (MPT splits a bare string on commas) or a list; normalise to a list.
    terms = out.get("video_terms")
    if isinstance(terms, str):
        cleaned_terms = [t.strip() for t in terms.split(",") if t.strip()]
    elif isinstance(terms, list):
        cleaned_terms = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
    else:
        cleaned_terms = []
    if cleaned_terms:
        out["video_terms"] = cleaned_terms
    else:
        out.pop("video_terms", None)

    # match_materials_to_script (B-7): order clips by narration and
    # round-robin downloads across the terms. With it on, MPT forces
    # sequential concat, and 8+ terms is the width that keeps the pool from
    # thinning out (S-2).
    out["match_materials_to_script"] = bool(out.get("match_materials_to_script", False))
    if out["match_materials_to_script"] and cleaned_terms and len(cleaned_terms) < 8:
        _log.warning(
            "job %s: match_materials_to_script=true with only %d video_terms — "
            "8+ terms keep the round-robin download pool from thinning out",
            out.get("name", "<unnamed>"),
            len(cleaned_terms),
        )

    # video_count: schema floor is 1; >1 silently forces random concat in MPT
    # (B-8), overriding video_concat_mode — warn rather than silently sending
    # a value that changes assembly behaviour.
    v_count = out.get("video_count")
    if isinstance(v_count, bool) or not isinstance(v_count, int) or v_count < 1:
        out.pop("video_count", None)
    elif v_count > 1:
        _log.warning(
            "job %s: video_count=%d makes MPT override video_concat_mode to random",
            out.get("name", "<unnamed>"),
            v_count,
        )

    # voice_volume: amplification in the final mix (not TTS loudness); MPT
    # has no bounds on it and values >1 clip without a limiter (N-5).
    v_vol = out.get("voice_volume")
    if isinstance(v_vol, bool) or not isinstance(v_vol, (int, float)):
        out.pop("voice_volume", None)

    # subtitle_display_mode: per-sentence or per-word subtitles
    if out.get("subtitle_display_mode") not in _VALID_DISPLAY_MODES:
        out["subtitle_display_mode"] = defaults.get("subtitle_display_mode", "sentence")

    # subtitle_animation: subtitle pop-in animation
    if out.get("subtitle_animation") not in _VALID_ANIMATIONS:
        out["subtitle_animation"] = defaults.get("subtitle_animation", "none")

    # image_motion (MPT 1.3.9): still/AI-image motion. Absent means the
    # server default (zoom = slow zoom) applies; "static" is the new lever for
    # a dead-still image — MPT documents Static + transition none as the pair
    # for that. Only rewrite invalid values, matching the whitelists above.
    if out.get("image_motion") not in _VALID_IMAGE_MOTIONS:
        out["image_motion"] = defaults.get("image_motion", "zoom")

    # rounded_subtitle_background: rounded corners on the subtitle plate.
    # Only renders together with a real text_background_color value (MPT
    # video.py); on its own it is a no-op.
    out["rounded_subtitle_background"] = bool(out.get("rounded_subtitle_background", False))
    # NOTE: subtitle_background_enabled / subtitle_background_color are
    # deliberately absent — VideoParams has no such fields (verified against
    # MoneyPrinterTurbo app/models/schema.py), so sending them meant silent
    # no-ops while the real lever, text_background_color, sat unused (B-2).
    # Drop them even when an older prompt or hand-copied example supplied them.
    out.pop("subtitle_background_enabled", None)
    out.pop("subtitle_background_color", None)

    # video_clip_speed: playback speed multiplier
    speed = out.get("video_clip_speed")
    if not isinstance(speed, (int, float)) or isinstance(speed, bool) or speed <= 0:
        out["video_clip_speed"] = _num_default(defaults, "video_clip_speed", 1.0)

    # video_music_prompt / sonilo_bgm_prompt: AI-generated music prompts
    # (pass-through, no range validation — prompt text is freeform)
    out.setdefault("video_music_prompt", defaults.get("video_music_prompt", ""))
    out.setdefault("sonilo_bgm_prompt", defaults.get("sonilo_bgm_prompt", ""))

    # custom_system_prompt: max 8000 chars (MPT schema constraint)
    csp = out.get("custom_system_prompt", "")
    if not isinstance(csp, str) or len(csp) > 8000:
        out["custom_system_prompt"] = defaults.get("custom_system_prompt", "")

    # video_materials: must be a list of dicts with provider/url/duration
    mats = out.get("video_materials")
    if mats is not None:
        if not isinstance(mats, list):
            out["video_materials"] = defaults.get("video_materials", [])
        else:
            cleaned = []
            for item in mats:
                if isinstance(item, dict) and item.get("provider"):
                    cleaned.append(
                        {
                            "provider": str(item["provider"]),
                            "url": str(item.get("url") or ""),
                            "duration": max(0, int((item.get("duration") or 0) or 0)),
                        }
                    )
            out["video_materials"] = cleaned or defaults.get("video_materials", [])

    # ponytail: always-on hook + optional duration-range guidance. A
    # video_script_prompt from job_defaults used to be overwritten wholesale
    # (B-5) — the only reachable lever over the script — so the built
    # instruction is appended on its own lines instead of replacing it.
    dur = defaults.get("duration_range")
    words: tuple[int, int | None] | None = None
    if dur:
        sec = parse_duration_range(dur)
        if sec:
            words = duration_to_words(sec)
            out["paragraph_number"] = max(out["paragraph_number"], paragraph_floor(sec))
    user_prompt = str(defaults.get("video_script_prompt", "") or "").strip()
    built_prompt = build_duration_instruction(words)
    out["video_script_prompt"] = f"{user_prompt}\n\n{built_prompt}" if user_prompt else built_prompt

    return out


# A subject this short can't fill a 30-45s video — treat it the same as a
# missing one (see fix #6: previously "" silently passed straight through).
_MIN_VIDEO_SUBJECT_CHARS = 30

# theme_mode emits short topic titles as video_subject, and topics mode imports
# whatever line the user supplied verbatim; the 30-char floor would reject both.
# Loosen it to "non-trivial" only for those two modes.
_SHORT_MIN_SUBJECT_CHARS = 5

# Strip leading list markers (1. 2) 3 - - * •) from topic lines so pasted
# numbered/bulleted lists don't leak markers into slugs and video_subjects.
# A leading digit that's part of the content ("5 mistakes everyone makes at work"
# — no punctuation after the digit) is preserved.
_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+\s*[.):\]\-]\s+|[-*•]\s+)")


def _strip_list_marker(line: str) -> str:
    """Remove a leading list marker (1. 2) 3 - - * •) from a topic line."""
    return _LIST_MARKER_RE.sub("", line, count=1).strip()


def _truncate_subject(subject: str, max_chars: int) -> str:
    """
    Truncate `subject` to at most `max_chars`, preferring a sentence
    boundary, then a word boundary, over a hard mid-word/mid-sentence cut
    (see fix #11: a plain subject[:max_chars] slice could previously leave
    a narration ending mid-word).
    """
    if len(subject) <= max_chars:
        return subject

    window = subject[:max_chars]

    # Prefer the last sentence-ending punctuation in the window, as long
    # as it isn't so early we'd throw away most of the allowed content.
    last_sentence_end = max(window.rfind("."), window.rfind("!"), window.rfind("?"))
    if last_sentence_end >= max_chars * 0.5:
        return window[: last_sentence_end + 1]

    # No good sentence boundary — fall back to the last word boundary so
    # we at least don't cut a word in half, and round it off with a period.
    last_space = window.rfind(" ")
    if last_space >= max_chars * 0.5:
        truncated = window[:last_space].rstrip()
        if truncated and truncated[-1] not in ".!?":
            truncated += "."
        return truncated

    # No usable boundary at all (e.g. one very long token) — hard cut.
    return window


def _normalise(
    job: dict,
    expected_suffix: str,
    lang_cfg: LangSettings | None = None,
    foreign_suffixes: set[str] | None = None,
    min_subject_chars: int = _MIN_VIDEO_SUBJECT_CHARS,
    max_subject_chars: int = VIDEO_SUBJECT_MAX_CHARS,
) -> dict:
    """
    Return a cleaned copy of job with:
    - enabled forced to True
    - path separators stripped from output_file
    - output_file lowercased, so dedup/seen-tracking can't be evaded by a
      case variant of an already-used name, and so two "different" queued
      filenames can't collide on a case-insensitive filesystem
    - missing file_suffix corrected on output_file. For the default
      (empty-suffix) language specifically, a *foreign* suffix accidentally
      present (e.g. "..._es.mp4" while generating for "en") is stripped
      instead — previously nothing validated this case at all, so such a
      file could be permanently misclassified by init-seen's suffix filter
    - video_subject clamped to max_subject_chars
    - voice/timing/bgm fields validated against lang_cfg (when provided),
      falling back to configured defaults instead of raising on bad LLM output

    Raises ValueError if video_subject is missing, empty, or too short to
    be a usable script — callers should treat that as a malformed job (skip,
    don't write), the same as a missing output_file.
    """
    output_file = job.get("output_file", "") or ""
    if not isinstance(output_file, str):
        output_file = str(output_file)

    # Strip any accidental path components (e.g. "subdir/fact.mp4" → "fact.mp4"),
    # then lowercase — see docstring above.
    output_file = Path(output_file).name.lower()

    stem = output_file.removesuffix(".mp4")
    if expected_suffix:
        # Ensure this lang's own suffix is present.
        suffix_lower = expected_suffix.lower()
        if not stem.endswith(suffix_lower):
            stem = f"{stem}{suffix_lower}"
    elif foreign_suffixes:
        for fs in foreign_suffixes:
            fs = (fs or "").lower()
            if fs and stem.endswith(fs) and len(stem) > len(fs):
                stem = stem[: len(stem) - len(fs)]
                break
    output_file = f"{stem}.mp4"

    subject = job.get("video_subject", "") or ""
    if not isinstance(subject, str):
        subject = str(subject)
    subject = subject.strip()
    if len(subject) < min_subject_chars:
        raise ValueError(
            f"video_subject is missing or too short ({len(subject)} chars, "
            f"need at least {min_subject_chars})"
        )
    if len(subject) > max_subject_chars:
        subject = _truncate_subject(subject, max_subject_chars)

    clean = {
        **job,
        "output_file": output_file,
        "enabled": True,
        "video_subject": subject,
    }

    if lang_cfg is not None:
        clean = _validate_against_config(clean, lang_cfg)

    return clean


def _deduplicate(
    raw_jobs: list[dict],
    already_known: set[str],
    lang_cfg: LangSettings | None = None,
    expected_suffix: str = "",
    foreign_suffixes: set[str] | None = None,
    min_subject_chars: int = _MIN_VIDEO_SUBJECT_CHARS,
    max_subject_chars: int = VIDEO_SUBJECT_MAX_CHARS,
    known_job_names: set[str] | None = None,
) -> list[dict]:
    # Lowercase for comparison: new output_file values are always already
    # lowercased by _normalise, but already_known (seen.txt + existing yaml
    # entries) may still hold case-varied names predating this fix, or from
    # a manual edit — comparing case-insensitively catches those too.
    known = {n.lower() for n in already_known}
    # Names already queued in the jobs file, so a generated job cannot reuse
    # one. Starting from an empty set made the uniqueness pass below blind to
    # everything already on disk.
    known_names: set[str] = set(known_job_names or ())
    result = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            _log.warning("malformed job (not an object): %r", job)
            continue

        output_file = job.get("output_file", "")
        if not output_file:
            _log.warning("job missing output_file: %r", job.get("name", "?"))
            continue

        try:
            # Normalise first so we dedup against the corrected filename.
            # A single malformed job (e.g. a null field where a string was
            # expected, or a missing/empty video_subject) must not take the
            # rest of the batch down with it.
            clean = _normalise(
                job,
                expected_suffix,
                lang_cfg,
                foreign_suffixes,
                min_subject_chars=min_subject_chars,
                max_subject_chars=max_subject_chars,
            )
        except Exception as e:
            _log.warning("malformed job %r: %s", job.get("name", "?"), e)
            continue

        output_file = clean["output_file"]  # already lowercased by _normalise

        if output_file in known:
            _log.info("skip duplicate: %s", output_file)
            continue

        if not clean.get("name") or not isinstance(clean.get("name"), str):
            clean = {**clean, "name": jobs.safe_name(output_file.replace(".mp4", ""))}

        # Keep job names unique too — the LLM only dedups output_file
        # against ALREADY USED TOPICS, so within a single batch it can
        # still produce two jobs that reduce to the same name.
        name = clean["name"]
        if name in known_names:
            base, i = name, 2
            while f"{base}_{i}" in known_names:
                i += 1
            name = f"{base}_{i}"
            clean = {**clean, "name": name}
        known_names.add(name)

        known.add(output_file)
        result.append(clean)
    return result


# ── Topics mode (no LLM) ───────────────────────────────────────────────────────


def _run_topics(
    lang: str,
    jobs_dir: Path,
    seen_dir: Path,
    settings,
    lang_cfg: LangSettings,
    topics: list[str],
) -> int:
    """
    Import a list of topics without LLM involvement. Each topic becomes a job
    with `video_subject` set to the topic text verbatim; other fields come from
    lang_cfg.job_defaults via the shared _deduplicate pipeline.
    """
    suffix = lang_cfg.file_suffix
    seen_path = seen.resolve(seen_dir, suffix)
    seen_set = seen.load(seen_path)
    cfg = jobs.load(jobs_dir, suffix, lang)
    already_known = seen_set | jobs.existing_names_from(cfg)
    foreign_suffixes = {
        c.file_suffix for code, c in settings.langs.items() if code != lang and c.file_suffix
    }

    print(
        f"[{lang}] topics mode: {len(topics)} topic(s) | jobs dir: {jobs_dir} | seen: {seen_path.name} (in {seen_dir})"  # noqa: E501
    )
    print(f"[{lang}] known titles: {len(already_known)}")

    raw_jobs = [
        {
            "name": jobs.safe_name(subject),
            "video_subject": subject,
            "output_file": f"{jobs.safe_name(subject)}{suffix}.mp4",
        }
        for t in topics
        if (subject := _strip_list_marker(t))
    ]

    clean_jobs = _deduplicate(
        raw_jobs,
        already_known,
        lang_cfg,
        expected_suffix=suffix,
        foreign_suffixes=foreign_suffixes,
        # Topics are imported verbatim, so a short one ("Octopus hearts") is a
        # valid topic, not a malformed job — the 30-char script floor would
        # drop it silently.
        min_subject_chars=_SHORT_MIN_SUBJECT_CHARS,
        known_job_names=jobs.existing_job_names_from(cfg),
    )
    print(f"[{lang}] after dedup: {len(clean_jobs)} new jobs")

    if clean_jobs:
        jobs.append(jobs_dir, suffix, lang, clean_jobs)
        jobs_file = jobs.new_path(jobs_dir, suffix).name
        print(f"[{lang}] appended {len(clean_jobs)} jobs to {jobs_file}")
    else:
        notify_alert(f"[{lang}] topics mode: 0 jobs added (all duplicates?)", settings)

    return len(clean_jobs)


# ── Core logic ────────────────────────────────────────────────────────────────

# If dedup leaves the queue still under threshold after a batch, try again
# a bounded number of extra times rather than silently stopping short — but
# give up early once an attempt yields nothing new, so a persistently
# unproductive LLM/prompt combination can't burn unbounded API calls.
_MAX_TOPUP_ATTEMPTS = 2
_MAX_JSON_PARSE_ATTEMPTS = 2


def _request_valid_jobs(
    prompt_for_count: Callable[[int], tuple[str, str]], settings: Settings, count: int
) -> tuple[list[dict[str, Any]], int]:
    """Return parsed jobs, splitting only requests that repeatedly return invalid JSON."""
    system_prompt, user_prompt = prompt_for_count(count)
    for attempt in range(_MAX_JSON_PARSE_ATTEMPTS):
        raw_text = call_llm(system_prompt, user_prompt, settings, count)
        try:
            return parse_json_array(raw_text), 0
        except ValueError as error:
            if attempt + 1 == _MAX_JSON_PARSE_ATTEMPTS:
                break
            print(f"  [retry] LLM returned invalid JSON ({error}), retrying...")
            time.sleep(1)
            user_prompt = (
                user_prompt.rstrip() + " (IMPORTANT: return ONLY valid JSON array, "
                "no prose, no explanation, no code fences)"
            )

    if count == 1:
        print("  [warning] LLM could not return one valid job after two attempts")
        return [], 1

    left_count = (count + 1) // 2
    right_count = count - left_count
    print(f"  [retry] splitting {count} jobs into {left_count} + {right_count} after invalid JSON")
    left_jobs, left_failed = _request_valid_jobs(prompt_for_count, settings, left_count)
    right_jobs, right_failed = _request_valid_jobs(prompt_for_count, settings, right_count)
    return left_jobs + right_jobs, left_failed + right_failed


def _lang_voice_errors(lang: str, lang_cfg: LangSettings) -> list[str]:
    """
    Pre-flight voice-pool validation for one language: the pool must be
    non-empty and every entry must be a voice MPT's dispatcher recognizes.
    Returns error lines (empty = fine). Testable on its own so execute()'s
    fail-fast and the verifier script share one implementation.
    """
    from mpt_autopilot.data import voice_data

    return voice_data.validate_pool(lang, lang_cfg.voices)


def run(
    lang: str,
    jobs_dir: Path | None,
    seen_dir: Path | None,
    force: bool,
    count_override: int | None,
    threshold_override: int | None,
    topics: list[str] | None = None,
    themes: list[str] | None = None,
    config_path: Path | None = None,
    duration_range: str | None = None,
) -> int:
    settings = load_settings(config_path, require_llm=topics is None)

    # Process-global, so it has to be (re)applied on entry to every stage that
    # writes a seen file — in `mpt run` the batch stage sets its own value in the
    # same process. Without this, pilot.seen_max_mb was read into Settings and
    # then never used by refill at all.
    seen.set_rotation_max_bytes(settings.seen_max_mb * 1024 * 1024)

    # Priority: explicit CLI flag > paths.* from config.yaml > current directory.
    jobs_dir = (jobs_dir or settings.jobs_dir or Path(".")).resolve()
    seen_dir = (seen_dir or settings.seen_dir or jobs_dir).resolve()

    if not jobs_dir.is_dir():
        raise FileNotFoundError(f"jobs directory not found: {jobs_dir}")

    lang_cfg = settings.lang(lang)

    # Voice pre-flight, before any LLM call: an empty or unknown-populated
    # langs.<lang>.voices is a config error, not something to half-handle
    # mid-generation (the voice assignment in _validate_against_config
    # silently skips empty pools, which is how hallucinated names once
    # reached jobs.yaml — see scripts/update_voice_data.py --verify-config).
    voice_errors = _lang_voice_errors(lang, lang_cfg)
    if voice_errors:
        raise ValueError("voice allowlist failed validation:\n  - " + "\n  - ".join(voice_errors))

    # CLI --duration-range overrides the per-language job_defaults value.
    # Rebasing lang_cfg once here covers every downstream reader (the system
    # prompt's duration claim, _validate_against_config's word-count guidance,
    # paragraph_number floor) without threading a new parameter through
    # _run_topics/_deduplicate/_normalise. Direct run() callers bypass
    # execute()'s early check, but the unconditional parse below still rejects
    # a bad value before any LLM call.
    if duration_range is not None:
        lang_cfg = dataclasses.replace(
            lang_cfg,
            job_defaults={**lang_cfg.job_defaults, "duration_range": duration_range},
        )
    suffix = lang_cfg.file_suffix

    # ponytail: validate duration_range before LLM call (topics mode uses _validate later).
    # The parsed value also feeds build_prompt so the system prompt's length claim
    # matches the word-count guidance _validate_against_config injects.
    duration_sec = parse_duration_range(lang_cfg.job_defaults.get("duration_range"))

    if topics:
        return _run_topics(lang, jobs_dir, seen_dir, settings, lang_cfg, topics)

    # Resolve active themes: --theme narrows to specific theme(s); no --theme
    # but non-empty config.theme_list uses all configured themes.
    configured = lang_cfg.theme_list
    if themes:
        unknown = [t for t in themes if t not in configured]
        if unknown:
            raise ValueError(
                f"--theme {unknown!r} not found in language config theme_list {configured!r}. "
                f"Add the theme(s) there first."
            )
        active_themes = list(dict.fromkeys(themes))  # dedupe, preserve order
    elif configured:
        active_themes = list(configured)
    else:
        active_themes = []

    if active_themes:
        print(f"[{lang}] theme mode: {active_themes}")

    seen_path = seen.resolve(seen_dir, suffix)
    seen_set = seen.load(seen_path)
    seen_list = seen_load_ordered(seen_path)

    threshold = threshold_override if threshold_override is not None else settings.refill_threshold
    generate_count = count_override if count_override is not None else settings.generate_count

    # --count was given on the CLI → one shot, no threshold top-up or full-queue guard
    count_explicit = count_override is not None

    # Load YAML once; derive both pending count and existing names from it.
    cfg = jobs.load(jobs_dir, suffix, lang)
    pending = jobs.count_pending_from(cfg, seen_set)

    seen_file = seen_path.name
    print(f"[{lang}] jobs dir: {jobs_dir}")
    print(
        f"[{lang}] pending jobs: {pending} | threshold: {threshold} | seen file: {seen_file} (in {seen_dir})"  # noqa: E501
    )

    if not count_explicit and pending >= threshold and not force:
        print(f"[{lang}] Queue is full — nothing to do. (Use --force to override.)")
        return 0

    existing = jobs.existing_names_from(cfg)
    already_known = seen_set | existing
    known_job_names = jobs.existing_job_names_from(cfg)
    print(f"[{lang}] known titles: {len(already_known)} | model: {settings.model}")

    # Other configured languages' suffixes, used so an output_file that
    # accidentally ends in e.g. "_es" while generating for a different
    # (or the default, empty-suffix) language gets caught — see fix #7
    # in _normalise().
    foreign_suffixes = {
        c.file_suffix for code, c in settings.langs.items() if code != lang and c.file_suffix
    }

    # seen_list only covers rendered videos (seen.txt). Topics already
    # queued in the jobs yaml but not yet rendered are just as much
    # "already used" from the LLM's point of view — without them the
    # prompt's dedup context misses the whole pending backlog, and the
    # LLM can propose near-duplicates of ideas that simply haven't been
    # rendered yet. Append pending-only names (not already in seen_list)
    # after seen_list so they're prioritised by the prompt's recency
    # slice (build_prompt keeps the *last* N entries). This list is grown
    # below with each top-up attempt's own new names too.
    existing_ordered = jobs.existing_names_ordered_from(cfg)
    pending_ordered = [n for n in existing_ordered if n not in seen_set]
    # R4: dedup against the TOPIC, not just the slug. Queued jobs know their
    # own video_subject — showing the idea text instead of (only) the filename
    # stops a semantically identical idea with a fresh slug from sailing
    # through. Rendered entries (seen.txt) only have filenames, so those stay
    # as-is.
    subjects_by_output = jobs.existing_subjects_by_output(cfg)
    prompt_seen_ordered = seen_list + [subjects_by_output.get(n) or n for n in pending_ordered]

    total_added = 0
    attempt = 0

    while True:
        attempt += 1
        is_topup = attempt > 1

        # The first call always uses the configured/overridden count as-is.
        # A top-up call asks for at least that many again, or however many
        # are still needed to cross the threshold if that's more.
        if is_topup:
            remaining_needed = max(threshold - (pending + total_added), 1)
            this_count = max(generate_count, remaining_needed)
            print(
                f"[{lang}] still under threshold ({pending + total_added}/{threshold}) "
                f"— topping up (attempt {attempt - 1}/{_MAX_TOPUP_ATTEMPTS})..."
            )
        else:
            this_count = generate_count

        def prompt_for_count(request_count: int) -> tuple[str, str]:
            if active_themes:
                return build_themes(
                    lang_cfg,
                    prompt_seen_ordered,
                    request_count,
                    themes=active_themes,
                )
            # R1-step2/A-3: with pilot.generation.visual_terms enabled the
            # same response carries a video_terms array per idea (one LLM
            # call, terms derived from the idea itself); the rules come from
            # config so they are visible and editable content.
            return build_prompt(
                lang_cfg,
                prompt_seen_ordered,
                request_count,
                duration_sec,
                visual_terms_rules=(
                    settings.visual_terms_rules if settings.visual_terms_enabled else None
                ),
            )

        # Theme mode asks for short titles, not scripts: loosen the floor and
        # apply the ceiling the theme prompt actually promises the model.
        subj_min = _SHORT_MIN_SUBJECT_CHARS if active_themes else _MIN_VIDEO_SUBJECT_CHARS
        subj_max = THEME_MAX_SUBJECT_CHARS if active_themes else VIDEO_SUBJECT_MAX_CHARS

        print(f"[{lang}] calling LLM for {this_count} ideas...")
        raw_jobs, failed_jobs = _request_valid_jobs(prompt_for_count, settings, this_count)

        if failed_jobs:
            warning = (
                f"[{lang}] WARNING: {failed_jobs}/{this_count} jobs could not be generated "
                "because the LLM repeatedly returned invalid JSON"
            )
            print(warning)
            notify_alert(warning, settings)

        if len(raw_jobs) < this_count:
            notify_alert(
                f"[{lang}] WARNING: LLM gave {len(raw_jobs)}/{this_count} jobs "
                f"(queue may stay low)",
                settings,
            )
            print(
                f"[{lang}] WARNING: LLM returned {len(raw_jobs)} of {this_count} requested — queue may still be low after this run"  # noqa: E501
            )

        print(f"[{lang}] LLM returned {len(raw_jobs)} raw jobs")

        clean_jobs = _deduplicate(
            raw_jobs,
            already_known,
            lang_cfg,
            expected_suffix=suffix,
            foreign_suffixes=foreign_suffixes,
            min_subject_chars=subj_min,
            max_subject_chars=subj_max,
            known_job_names=known_job_names,
        )
        print(f"[{lang}] after dedup: {len(clean_jobs)} new jobs")

        if clean_jobs:
            jobs.append(jobs_dir, suffix, lang, clean_jobs)
            jobs_file = jobs.new_path(jobs_dir, suffix).name
            print(f"[{lang}] appended {len(clean_jobs)} jobs to {jobs_file}")
            total_added += len(clean_jobs)

            # Feed this attempt's new topics into the next attempt's dedup
            # context, so a top-up round doesn't just re-propose what the
            # previous round already added (subjects preferred over slugs,
            # same as the initial context above).
            new_names = [j["output_file"] for j in clean_jobs]
            already_known |= set(new_names)
            known_job_names |= {j["name"] for j in clean_jobs}
            prompt_seen_ordered += [
                str(j.get("video_subject", "")).strip()[:160] or j["output_file"]
                for j in clean_jobs
            ]

        if count_explicit or pending + total_added >= threshold:
            break
        if not clean_jobs:
            if is_topup:
                print(f"[{lang}] top-up attempt produced nothing new — stopping")
            break
        if attempt > _MAX_TOPUP_ATTEMPTS:
            break

    if total_added == 0:
        print(f"[{lang}] nothing new after dedup — try again or use --force")
        return 0

    # Note: the seen registry is updated by the batch stage after each video is
    # rendered, not here — refill only writes to the jobs yaml.
    notify_alert(
        f"[{lang}] done: {total_added} new jobs (pending {pending + total_added})",
        settings,
    )
    return total_added


# ── CLI ───────────────────────────────────────────────────────────────────────


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register the pilot refill flags on `parser`.

    Kept separate from build_parser() so `mpt`'s subparser and a standalone
    parser share one definition — the CLI owns `--config` at the root level, so
    it is deliberately not registered here.
    """
    parser.add_argument(
        "--lang",
        required=True,
        metavar="LANG",
        help="Language code (e.g. en, es). Must be defined in config.yaml.",
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Directory containing jobs.yaml / jobs_<suffix>.yaml files. "
        "Default: paths.jobs_dir from config.yaml, else current directory.",
    )
    parser.add_argument(
        "--seen-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Directory for seen_<lang>.txt files. "
        "Default: paths.seen_dir from config.yaml, else --jobs-dir.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Refill even if the queue is above the threshold."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Override pilot.generation.count from config.yaml.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        metavar="N",
        help="Override pilot.generation.threshold from config.yaml.",
    )
    parser.add_argument(
        "--duration-range",
        type=str,
        default=None,
        metavar="RANGE",
        help="Narration target in seconds, 'MIN-MAX' or 'MIN+'. Overrides "
        "langs.<code>.job_defaults.duration_range from config.yaml. Applies to "
        "--topic/--topics and --theme modes too.",
    )
    parser.add_argument(
        "--topic",
        action="append",
        dest="topic",
        default=None,
        metavar="TOPIC",
        help="Generate a job for this specific topic (no LLM). Topic text becomes "
        "video_subject verbatim. Repeatable. Combined with --topics.",
    )
    parser.add_argument(
        "--topics",
        type=Path,
        dest="topics_file",
        default=None,
        metavar="FILE",
        help="File (UTF-8, one topic per line, blank lines ignored) imported as jobs. "
        "Combined with --topic.",
    )
    parser.add_argument(
        "--theme",
        action="append",
        dest="theme",
        default=None,
        metavar="THEME",
        help="LLM theme mode: generate short topic titles constrained to this "
        "theme from this language's theme_list in config.yaml. Repeatable. "
        "Without --theme but with a non-empty theme_list in the language's "
        "config block, all configured themes are used.",
    )
    return parser


EPILOG = """
Examples:
    mpt refill --lang en
    mpt refill --lang en --jobs-dir /your/path/to/jobs
    mpt refill --lang en --jobs-dir /your/path/to/jobs --force
    mpt refill --lang en --jobs-dir /your/path/to/jobs --count 50
    mpt refill --lang en --jobs-dir /your/path/to/jobs --threshold 5

    # Import specific topics (no LLM):
    mpt refill --lang en --topic "The tongue is the strongest muscle in your body"
    mpt refill --lang en --topic "Antarctica is the driest desert" --topic "Octopuses have three hearts"
    mpt refill --lang en --topics /path/to/topics.txt

    # Theme mode (LLM generates short titles about configured themes):
    mpt refill --lang en --force --count 20      # all themes from the language's theme_list
    mpt refill --lang en --theme job --force --count 5  # only themes matching "job"
"""  # noqa: E501


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt refill",
        description="Auto-refill your MoneyPrinterTurbo jobs queue with LLM-generated video ideas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Config file path (default: config.yaml)",
    )
    return add_arguments(parser)


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    """
    Run the pilot refill stage from already-parsed arguments.

    Returns an exit code instead of calling sys.exit so `mpt run` can aggregate
    stages in one process.
    """
    if config_path is None:
        config_path = getattr(args, "config", None)

    if args.count is not None and args.count <= 0:
        print("[ERROR] --count must be a positive integer")
        return 1
    if args.threshold is not None and args.threshold < 0:
        print("[ERROR] --threshold must be a non-negative integer")
        return 1
    # getattr: execute() is also called with hand-built Namespaces (pipeline,
    # tests) that predate the flag — same shape as pipeline's M-55 pattern.
    duration_flag = getattr(args, "duration_range", None)
    if duration_flag is not None:
        try:
            parse_duration_range(duration_flag)
        except ValueError as e:
            print(f"[ERROR] --duration-range: {e}")
            return 1

    # Topics mode: build list from --topic (repeatable) + --topics (file).
    topic_file_lines: list[str] = []
    if args.topics_file is not None:
        if not args.topics_file.is_file():
            print(f"[ERROR] --topics file not found: {args.topics_file}")
            return 1
        topic_file_lines = [
            ln.strip()
            for ln in args.topics_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    inline_topics = [t for t in (args.topic or []) if t.strip()]
    topics_requested = args.topics_file is not None or bool(args.topic)
    topics = (topic_file_lines + inline_topics) if topics_requested else None
    if topics_requested and not topics:
        print("[ERROR] topics requested but none found (empty file and/or blank --topic values)")
        return 1
    if topics and args.theme:
        print("[ERROR] --topic/--topics (verbatim, no LLM) and --theme (LLM) can't be combined")
        return 1
    if topics and (args.count is not None or args.threshold is not None or args.force):
        print("[note] --count / --threshold / --force ignored in topics mode")

    # themes from --theme (repeatable) → passed to run() (none means "use config or free-topic")
    theme_list = [t for t in (args.theme or []) if t.strip()] or None

    jobs_dir = args.jobs_dir.resolve() if args.jobs_dir else None
    seen_dir = args.seen_dir.resolve() if args.seen_dir else None

    try:
        added = run(
            lang=args.lang,
            jobs_dir=jobs_dir,
            seen_dir=seen_dir,
            force=args.force,
            count_override=args.count,
            threshold_override=args.threshold,
            topics=topics,
            themes=theme_list,
            config_path=config_path,
            duration_range=duration_flag,
        )
    except Exception as e:
        try:
            settings = load_settings(config_path, require_llm=False)
            notify_alert(f"[{args.lang}] FAILED: {e}", settings)
        except Exception:
            # A broken/missing config must not mask the original failure.
            pass
        print(f"[ERROR] {e}")
        return 1

    print(f"\n[done] added {added} new jobs.")
    return 0


def main() -> None:
    # Give the root logger a handler before any stage runs (M-1).
    configure_root()
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
