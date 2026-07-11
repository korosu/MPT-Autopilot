"""
generator/prompt.py

Builds the system + user prompts sent to the LLM.
Kept separate so the prompt can be tuned without touching any other logic.
"""

from __future__ import annotations

import json

from shorts_pilot.generator.settings import LangSettings

VIDEO_SUBJECT_MAX_CHARS = 500

# How many recent seen entries to include in the prompt.
# Uses insertion order (most recently generated = last in file).
_MAX_SEEN_IN_PROMPT = 200
_THEME_MAX_SUBJECT_CHARS = 80  # For short titles in theme mode


def _seen_block(seen_ordered: list[str]) -> str:
    """Render the ALREADY USED TOPICS block for prompts (shared by both modes)."""
    recent = seen_ordered[-_MAX_SEEN_IN_PROMPT:] if seen_ordered else []
    return "\n".join(n.replace(".mp4", "") for n in recent) if recent else "(none yet)"


def _prompt_defaults(lang_cfg: LangSettings) -> dict:
    """Extract shared prompt defaults from lang_cfg to avoid duplication."""
    defaults = lang_cfg.job_defaults
    return {
        "suffix": lang_cfg.file_suffix,
        "voices_json": json.dumps(lang_cfg.voices),
        "clip_duration": defaults.get("video_clip_duration", 3),
        "bgm_volume": defaults.get("bgm_volume", 0.15),
        "paragraph_number": defaults.get("paragraph_number", 2),
        "concat_mode": defaults.get("video_concat_mode", "random"),
        "bgm_type": defaults.get("bgm_type", "random"),
    }


def build(
    lang_cfg: LangSettings,
    seen_ordered: list[str],
    count: int,
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for the given language and context."""
    used_str = _seen_block(seen_ordered)
    d = _prompt_defaults(lang_cfg)

    example = {
        "name": f"fact_tongue_strongest_muscle{d['suffix']}",
        "enabled": True,
        "output_file": f"fact_tongue_strongest_muscle{d['suffix']}.mp4",
        "video_subject": (
            "Your tongue is not the strongest muscle in your body. "
            "That title goes to the masseter jaw muscle. "
            "The tongue myth keeps spreading because it sounds believable."
        ),
        "video_clip_duration": d["clip_duration"],
        "video_concat_mode": d["concat_mode"],
        "voice_rate": 1.15,
        "voice_name": lang_cfg.voices[0] if lang_cfg.voices else "gemini:puck",
        "bgm_type": d["bgm_type"],
        "bgm_volume": d["bgm_volume"],
        "paragraph_number": d["paragraph_number"],
    }

    system = (
        f"You are a YouTube Shorts content strategist.\n"
        f"Your job is to generate viral fact/myth-busting short video scripts in {lang_cfg.label}.\n"  # noqa: E501
        f"Each video is 30–45 seconds long, spoken in a direct, engaging voice.\n"
        f"Return ONLY a valid JSON array. No markdown, no explanation, no code fences."
    )

    user = f"""Generate exactly {count} NEW YouTube Shorts video jobs in {lang_cfg.label}.

ALREADY USED TOPICS — do not repeat any of these:
{used_str}

Rules:
- Topics: surprising facts, common myths debunked, counterintuitive science, \
historical misconceptions, animal facts, body facts, space facts, psychology, \
food science. Wide variety. Never repeat a topic from the list above.
- video_subject: 3–5 sentences in {lang_cfg.label}, conversational and punchy. \
State the surprising fact, explain WHY, end with a memorable kicker. \
No hashtags, no emojis. Maximum {VIDEO_SUBJECT_MAX_CHARS} characters — be concise.
- name and output_file: snake_case, max 50 chars. \
output_file must end with "{d["suffix"]}.mp4" (e.g. "fact_water_memory{d["suffix"]}.mp4").
- voice_rate: float between {lang_cfg.voice_rate_min} and {lang_cfg.voice_rate_max}, vary across jobs.  # noqa: E501
- voice_name: pick from {d["voices_json"]}, vary across jobs.
- video_clip_duration: {d["clip_duration"]} or {d["clip_duration"] + 1}.
- paragraph_number: 1 or {d["paragraph_number"]}.
- bgm_volume: {d["bgm_volume"]}.
- bgm_type: "{d["bgm_type"]}".
- video_concat_mode: "{d["concat_mode"]}".
- enabled: true.

Example of ONE object (do not copy it — generate fresh, original content):
{json.dumps(example, indent=2, ensure_ascii=False)}

Return ONLY a JSON array of exactly {count} objects. No markdown. No commentary.
"""

    return system, user


def build_themes(
    lang_cfg: LangSettings,
    seen_ordered: list[str],
    count: int,
    themes: list[str],
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for theme-constrained generation."""
    used_str = _seen_block(seen_ordered)
    d = _prompt_defaults(lang_cfg)
    themes_str = ", ".join(themes)

    example = {
        "name": f"why_people_hate_mondays{d['suffix']}",
        "enabled": True,
        "output_file": f"why_people_hate_mondays{d['suffix']}.mp4",
        "video_subject": "Why people hate Mondays",
        "video_clip_duration": d["clip_duration"],
        "video_concat_mode": d["concat_mode"],
        "voice_rate": 1.15,
        "voice_name": lang_cfg.voices[0] if lang_cfg.voices else "gemini:puck",
        "bgm_type": d["bgm_type"],
        "bgm_volume": d["bgm_volume"],
        "paragraph_number": d["paragraph_number"],
    }

    system = (
        f"You are a YouTube Shorts content strategist.\n"
        f"Your job is to generate SHORT, punchy video TOPIC TITLES in {lang_cfg.label} "
        f"about specific themes.\n"
        f"Each title is the seed topic for one video (3–12 words); the script is "
        f"generated downstream — here you only pick the topic.\n"
        f"Return ONLY a valid JSON array. No markdown, no explanation, no code fences."
    )

    user = f"""Generate exactly {count} NEW YouTube Shorts topic titles in {lang_cfg.label}.

THEMES — every title must be about ONE of these themes ({themes_str}):
Pick across the themes from your list for variety. Do NOT produce a title outside
these themes.

ALREADY USED TOPICS — do not repeat any of these:
{used_str}

Rules:
- video_subject: a short topic title, 3–12 words, max {_THEME_MAX_SUBJECT_CHARS} characters.
  Title case or sentence case. No hashtags, no emojis, no trailing period. Must relate
  to one of the listed themes.
- name and output_file: snake_case, max 50 chars.
  output_file must end with "{d["suffix"]}.mp4" (e.g. "fact_water_memory{d["suffix"]}.mp4").
- voice_rate: float between {lang_cfg.voice_rate_min} and {lang_cfg.voice_rate_max}, vary across jobs.  # noqa: E501
- voice_name: pick from {d["voices_json"]}, vary across jobs.
- video_clip_duration: {d["clip_duration"]} or {d["clip_duration"] + 1}.
- paragraph_number: 1 or {d["paragraph_number"]}.
- bgm_volume: {d["bgm_volume"]}.
- bgm_type: "{d["bgm_type"]}".
- video_concat_mode: "{d["concat_mode"]}".
- enabled: true.

Example of ONE object (do not copy it — generate fresh titles on the listed themes):
{json.dumps(example, indent=2, ensure_ascii=False)}

Return ONLY a JSON array of exactly {count} objects. No markdown. No commentary.
"""

    return system, user
