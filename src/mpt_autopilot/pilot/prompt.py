"""
pilot/prompt.py

Builds the system + user prompts sent to the LLM.
Kept separate so the prompt can be tuned without touching any other logic.
"""

from __future__ import annotations

import json

from mpt_autopilot.pilot.jobs import NAME_MAX_CHARS
from mpt_autopilot.pilot.settings import LangSettings

VIDEO_SUBJECT_MAX_CHARS = 500

_MAX_SEEN_IN_PROMPT = 200
# Theme mode asks for short titles, not scripts. refill._deduplicate enforces
# this as the clamp for theme mode; a prompt rule nothing checks is a
# suggestion, and the model exceeded it regularly.
THEME_MAX_SUBJECT_CHARS = 80

# What the system prompt claims about video length when no duration_range is
# configured. With one configured, build() states that range instead — telling
# the model "30–45 seconds" while video_script_prompt asks for 60–90 gives it
# two incompatible targets.
_DEFAULT_DURATION_PHRASE = "30–45 seconds"


def _duration_phrase(duration: tuple[int, int | None] | None) -> str:
    if duration is None:
        return _DEFAULT_DURATION_PHRASE
    lo, hi = duration
    return f"{lo}–{hi} seconds" if hi is not None else f"at least {lo} seconds"


def _seen_block(seen_ordered: list[str]) -> str:
    """Render the ALREADY USED TOPICS block for prompts (shared by both modes)."""
    recent = seen_ordered[-_MAX_SEEN_IN_PROMPT:] if seen_ordered else []
    return "\n".join(n.removesuffix(".mp4") for n in recent) if recent else "(none yet)"


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
        # New upstream fields (MPT v1.3.6 VideoParams)
        "fit_mode": defaults.get("video_fit_mode", "cover"),
        "display_mode": defaults.get("subtitle_display_mode", "sentence"),
        "animation": defaults.get("subtitle_animation", "none"),
    }


def build(
    lang_cfg: LangSettings,
    seen_ordered: list[str],
    count: int,
    duration: tuple[int, int | None] | None = None,
    visual_terms_rules: str | None = None,
) -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) for the given language and context.

    `duration` is the parsed job_defaults.duration_range, when configured, so
    the system prompt's length claim matches the word-count guidance refill
    injects into video_script_prompt.

    `visual_terms_rules` (R1-step2/A-3) is the configured rule text for the
    video_terms field — None/empty keeps the prompt exactly as it was. The
    rules live in config.yaml (pilot.generation.visual_terms_rules), not in
    code: an inline prompt nobody sees degrades silently (B-10's lesson).
    """
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
        "video_script": "",
        "bgm_type": d["bgm_type"],
        "bgm_volume": d["bgm_volume"],
        "paragraph_number": d["paragraph_number"],
        "video_fit_mode": d["fit_mode"],
        "subtitle_display_mode": d["display_mode"],
        "subtitle_animation": d["animation"],
        "video_clip_speed": 1.0,
    }
    if visual_terms_rules:
        # Terms are ALWAYS English (stock APIs search English tags; MPT's own
        # generator requires the same), independent of the video language.
        example["video_terms"] = [
            "human tongue closeup",
            "jaw muscle anatomy diagram",
            "person chewing slow motion",
            "tongue taste buds macro",
            "anatomy textbook page",
            "doctor pointing at skull model",
            "medical animation",
            "human anatomy",
        ]

    system = (
        f"You are a YouTube Shorts content strategist.\n"
        f"Your job is to generate viral fact/myth-busting short video scripts in {lang_cfg.label}.\n"  # noqa: E501
        f"Each video is {_duration_phrase(duration)} long, spoken in a direct, engaging voice.\n"
        f"Each script opens with a single-sentence HOOK so the viewer stays past the 3-second mark.\n"
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
Sentence 1 MUST be a hook that grabs the viewer in the first 3–5 seconds—a single \
shocking claim, counterintuitive observation, teaser question, or vivid image \
(no greetings, no "did you know", no throat-clearing). Sentences 2–4 deliver the \
surprising fact and explain WHY. End with a memorable kicker. No hashtags, no emojis. \
Maximum {VIDEO_SUBJECT_MAX_CHARS} characters — be concise.
- name and output_file: snake_case, max {NAME_MAX_CHARS} chars. \
output_file must end with "{d["suffix"]}.mp4" (e.g. "fact_water_memory{d["suffix"]}.mp4").
- voice_rate: float between {lang_cfg.voice_rate_min} and {lang_cfg.voice_rate_max}, vary across jobs.
- voice_name: pick from {d["voices_json"]}, vary across jobs."""
    if visual_terms_rules:
        user += f"""- video_terms: a JSON array of stock-search keywords. Rules:
{visual_terms_rules}
"""
    user += f"""- video_clip_duration: {d["clip_duration"]} or {d["clip_duration"] + 1}.
- paragraph_number: 1 or {d["paragraph_number"]}.
- bgm_volume: {d["bgm_volume"]}.
- bgm_type: "{d["bgm_type"]}".
- video_concat_mode: "{d["concat_mode"]}".
- video_fit_mode: "{d["fit_mode"]}" ("cover" crops to fill; "contain" adds black bars).
- subtitle_display_mode: "{d["display_mode"]}" ("sentence" or "word_by_word").
- subtitle_animation: "{d["animation"]}" ("none" or "pop_spring").
- video_clip_speed: 1.0 (use 0.8–1.2 for slow/fast motion).
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
        "video_script": "",
        "bgm_type": d["bgm_type"],
        "bgm_volume": d["bgm_volume"],
        "paragraph_number": d["paragraph_number"],
        "video_fit_mode": d["fit_mode"],
        "subtitle_display_mode": d["display_mode"],
        "subtitle_animation": d["animation"],
        "video_clip_speed": 1.0,
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
- video_subject: a short topic title, 3–12 words, max {THEME_MAX_SUBJECT_CHARS} characters.
  Title case or sentence case. No hashtags, no emojis, no trailing period. Must relate
  to one of the listed themes.
- name and output_file: snake_case, max {NAME_MAX_CHARS} chars.
  output_file must end with "{d["suffix"]}.mp4" (e.g. "fact_water_memory{d["suffix"]}.mp4").
- voice_rate: float between {lang_cfg.voice_rate_min} and {lang_cfg.voice_rate_max}, vary across jobs.
- voice_name: pick from {d["voices_json"]}, vary across jobs.
- video_clip_duration: {d["clip_duration"]} or {d["clip_duration"] + 1}.
- paragraph_number: 1 or {d["paragraph_number"]}.
- bgm_volume: {d["bgm_volume"]}.
- bgm_type: "{d["bgm_type"]}".
- video_concat_mode: "{d["concat_mode"]}".
- video_fit_mode: "{d["fit_mode"]}" ("cover" crops to fill; "contain" adds black bars).
- subtitle_display_mode: "{d["display_mode"]}" ("sentence" or "word_by_word").
- subtitle_animation: "{d["animation"]}" ("none" or "pop_spring").
- video_clip_speed: 1.0 (use 0.8–1.2 for slow/fast motion).
- enabled: true.

Example of ONE object (do not copy it — generate fresh titles on the listed themes):
{json.dumps(example, indent=2, ensure_ascii=False)}

Return ONLY a JSON array of exactly {count} objects. No markdown. No commentary.
"""

    return system, user
