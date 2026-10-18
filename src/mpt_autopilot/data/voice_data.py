"""
mpt_autopilot.data.voice_data

Reference data about the voices MoneyPrinterTurbo's TTS dispatcher accepts,
bundled as JSON next to this module so every stage (and the verifier script)
shares one source of truth instead of each keeping its own copy:

- edge_voices.json — the free Edge TTS voices (regenerated from MPT's
  docs/voice-list.txt by scripts/update_voice_data.py)
- gemini_voices.json — the Gemini TTS prebuilt voices (regenerated from the
  GEMINI_TTS_VOICES constant in MPT's app/services/voice.py)

The classifier mirrors MPT's dispatch (voice.py tts() is prefix-based):
"gemini:" → Gemini list, bare Edge-style names → Edge list, other known
provider prefixes (siliconflow:/mimo:/minimax:/elevenlabs:/chatterbox:/
kokoro:/fish_audio:, trailing "-V2" for Azure v2) → accepted as "provider
voice, not locally verifiable", anything else → unknown.

Gemini names compare case-insensitively on the token before the first "-":
MPT's parse_gemini_voice_name() strips styles/genders that way and sends the
result verbatim to the Google API, which is case-insensitive — 780+ rendered
videos with lowercase "gemini:puck" prove it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).parent

# Provider prefixes MPT's tts() dispatches on (voice.py). A voice with one of
# these prefixes is accepted without local verification — its account/key
# lives on the MPT server, not in this repo's reference data.
_PROVIDER_PREFIXES = (
    "siliconflow:",
    "mimo:",
    "minimax:",
    "elevenlabs:",
    "chatterbox:",
    "kokoro:",
    "fish_audio:",
)

# Sentinels MPT treats as "no narration at all" (voice.py NO_VOICE_NAME and
# its legacy alias "none"); such a job renders without TTS.
NO_VOICE = "no-voice"
_NO_VOICE_NAMES = frozenset({NO_VOICE, "none"})


@lru_cache(maxsize=1)
def _edge_entries() -> tuple[dict[str, str], ...]:
    with open(_DATA_DIR / "edge_voices.json", encoding="utf-8") as f:
        loaded = json.load(f)
    entries = tuple(e for e in loaded if isinstance(e, dict) and e.get("name"))
    return entries


@lru_cache(maxsize=1)
def _gemini_names() -> tuple[str, ...]:
    with open(_DATA_DIR / "gemini_voices.json", encoding="utf-8") as f:
        loaded = json.load(f)
    return tuple(v for v in loaded if isinstance(v, str) and v.strip())


def edge_voice_entries() -> tuple[dict[str, str], ...]:
    """Raw bundled Edge entries as {name, gender} dicts (batch aliases need gender)."""
    return _edge_entries()


def edge_voices() -> tuple[str, ...]:
    """Bundled free Edge TTS voice names, e.g. "es-ES-ElviraNeural"."""
    return tuple(e["name"] for e in _edge_entries())


def gemini_voices() -> tuple[str, ...]:
    """The prebuilt Gemini TTS voice names, e.g. "Puck", "Charon"."""
    return _gemini_names()


@lru_cache(maxsize=1)
def _lookups() -> tuple[frozenset[str], frozenset[str]]:
    # Both lazy so `import mpt_autopilot` performs no file reads — the CLI
    # must start without touching data files (same reason batch/voices.py
    # caches instead of loading at module scope).
    gemini = frozenset(v.lower() for v in _gemini_names())
    edge = frozenset(edge_voices())
    return gemini, edge


def classify(voice_name: str) -> str:
    """
    Classify a voice_name the way MPT's TTS dispatcher will route it.

    Returns one of:
    - "gemini"   — "gemini:<name>" where <name> (before any "-") is a known
                   prebuilt voice; case-insensitive.
    - "edge"     — a bare name present in the bundled Edge list (the
                   "-Female"/"-Male" suffix MPT's parse_voice_name strips is
                   optional for this check).
    - "provider" — carries a known paid-provider prefix; validity depends on
                   the MPT server's own configuration, accepted as-is.
    - "azure-v2" — Edge-style name with a trailing "-V2" (Azure v2 path);
                   accepted as-is, same reasoning as "provider".
    - "no-voice" — the explicit no-narration sentinels.
    - "unknown"  — none of the above: MPT would route it to free Edge TTS,
                   which rejects unknown names instantly (exactly how the
                   "espeak:en"/"azure:…" hallucinations failed).
    """
    name = (voice_name or "").strip()
    if not name:
        return "unknown"

    if name.startswith("gemini:"):
        token = name.split(":", 1)[1].split("-", 1)[0].strip().lower()
        return "gemini" if token and token in _lookups()[0] else "unknown"

    if name in _NO_VOICE_NAMES:
        return "no-voice"

    if name.startswith(_PROVIDER_PREFIXES):
        return "provider"

    bare = name
    for suffix in ("-Female", "-Male"):
        if bare.endswith(suffix):
            bare = bare[: -len(suffix)]
    if bare.endswith("-V2"):
        return "azure-v2"

    edge = _lookups()[1]
    return "edge" if name in edge or bare in edge else "unknown"


def is_known(voice_name: str) -> bool:
    """True when classify() proves the voice locally (anything but "unknown")."""
    return classify(voice_name) != "unknown"


def describe_status(voice_name: str) -> str:
    """One-line human explanation for --verify-config output."""
    status = classify(voice_name)
    return {
        "gemini": "gemini prebuilt voice (verified against bundled list)",
        "edge": "edge free voice (verified against bundled list)",
        "provider": "provider voice (accepted; your MPT server verifies it at render time)",
        "azure-v2": "azure-v2 voice (accepted; your MPT server verifies it at render time)",
        "no-voice": "no-voice sentinel (job renders without narration)",
    }.get(status, "UNKNOWN — not a bundled Edge or Gemini voice and no provider prefix")


def validate_pool(code: str, voices: list[str]) -> list[str]:
    """
    Validate one language's voices allowlist. Returns human-readable error
    lines (empty list = pool is fine).

    Unknown entries are errors, not warnings: the allowlist exists precisely
    so hallucinated names never reach jobs.yaml, so the allowlist itself must
    not contain any.
    """
    if not voices:
        return [
            f"langs.{code}.voices is empty — the voice allowlist is required: "
            f"pilot assigns every job's voice from this pool, and without it "
            f"the old failure mode (LLM-invented names failing at render) "
            f"comes back. List at least one voice."
        ]
    return [
        f"langs.{code}.voices: {v!r} — {describe_status(v)}. Fix the name or "
        f"regenerate the reference data (scripts/update_voice_data.py)."
        for v in voices
        if classify(v) == "unknown"
    ]
