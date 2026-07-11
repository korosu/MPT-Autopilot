"""
engine/voices.py

Optional convenience layer on top of MoneyPrinterTurbo's voice fields.

`voice_name` is what actually selects a voice. Current mainline
MoneyPrinterTurbo (app/services/voice.py) dispatches purely on the shape of
`voice_name`:

    - ends with "-V2"        -> Azure TTS V2 (needs an Azure Speech key)
    - "siliconflow:..."      -> SiliconFlow
    - "gemini:..."           -> Gemini TTS   (needs a Gemini API key)
    - "mimo:..."             -> Xiaomi MiMo TTS
    - "elevenlabs:..."       -> ElevenLabs
    - anything else          -> Edge TTS / "Azure TTS V1" (free, e.g. "es-ES-ElviraNeural")

`tts_server` is kept here for readability and to mirror the WebUI's field,
but it doesn't appear anywhere in MoneyPrinterTurbo's own task payload logs
— the API dispatches on voice_name alone. Harmless to set, just don't expect
it to change anything by itself.

Each preset in config.yaml's `voices:` section resolves a short alias to
BOTH fields together so they can never drift out of sync:

    voices:
      gemini:
        gemini_puck:
          tts_server: "gemini"
          voice_name: "gemini:puck"

Then in jobs.yaml:

    defaults:
      voice: "gemini_puck"

On top of whatever you define in config.yaml, this module also auto-loads
every free Edge TTS voice (314 of them, all languages) from the bundled
data/edge_voices.json, so they're usable as aliases without any config.yaml
edits — e.g. "es_es_elvira" for es-ES-ElviraNeural (Female). Run
`uv run batch --list-voices es` to browse/search them. config.yaml aliases
win if a name collides with a bundled one.

This module only resolves the alias to the underlying field(s) — it has no
opinion about which provider is "best" and does not validate that a paid
provider is actually configured on your MoneyPrinterTurbo server (see README).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Fields a voice preset is allowed to set on the API payload.
_ALLOWED_FIELDS = {"tts_server", "voice_name", "voice_rate", "voice_volume"}

_EDGE_VOICES_DATA_FILE = Path(__file__).parent.parent / "data" / "edge_voices.json"
_edge_voices_cache: list[dict] | None = None


def _load_edge_voices() -> list[dict]:
    """Load the bundled {name, gender} list, generated from MoneyPrinterTurbo's
    own Edge TTS voice list (docs/voice-list.txt). Cached after first read."""
    global _edge_voices_cache
    if _edge_voices_cache is None:
        with open(_EDGE_VOICES_DATA_FILE, encoding="utf-8") as f:
            _edge_voices_cache = json.load(f)
    return _edge_voices_cache


def alias_for_edge_voice(name: str) -> str:
    """
    "es-ES-ElviraNeural" -> "es_es_elvira"
    Strips the trailing "Neural", lowercases, and swaps any remaining
    non-alphanumeric run for a single underscore. Verified collision-free
    across all 314 bundled voices.
    """
    base = name[:-6] if name.endswith("Neural") else name
    return re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")


def build_edge_pool() -> dict[str, dict]:
    """
    Build the {alias: {tts_server, voice_name}} pool for every bundled Edge
    TTS voice. voice_name keeps the "-Female"/"-Male" suffix MoneyPrinterTurbo's
    own parse_voice_name() expects and strips before calling edge_tts.
    """
    pool: dict[str, dict] = {}
    for voice in _load_edge_voices():
        alias = alias_for_edge_voice(voice["name"])
        pool[alias] = {
            "tts_server": "edge",
            "voice_name": f"{voice['name']}-{voice['gender']}",
        }
    return pool


def build_pool(voices_cfg: dict) -> dict[str, dict]:
    """
    Flatten config.yaml's `voices:` section into a single {alias: {tts_server,
    voice_name, ...}} pool. The top-level provider keys (gemini, ...) exist
    only for readability — aliases must be unique across all of them.
    Returns an empty pool if the section is missing or empty (voices are optional).
    """
    pool: dict[str, dict] = {}
    for provider, aliases in (voices_cfg or {}).items():
        if not isinstance(aliases, dict):
            continue
        for alias, fields in aliases.items():
            if alias in pool:
                raise ValueError(
                    f"config.yaml: voice alias '{alias}' is defined more than once "
                    f"(last seen under voices.{provider}) — alias names must be unique."
                )
            unknown = set(fields) - _ALLOWED_FIELDS
            if unknown:
                raise ValueError(
                    f"config.yaml: voice alias '{alias}' under voices.{provider} has "
                    f"unsupported field(s) {sorted(unknown)} — allowed: {sorted(_ALLOWED_FIELDS)}"
                )
            pool[alias] = dict(fields)

    return pool


def build_full_pool(voices_cfg: dict) -> dict[str, dict]:
    """
    The pool actually used at runtime: every bundled Edge TTS voice, plus
    whatever config.yaml defines on top. config.yaml wins on name collisions,
    so you can override a bundled voice's rate/volume by redefining its alias.
    """
    return {**build_edge_pool(), **build_pool(voices_cfg)}


def resolve(payload: dict, pool: dict[str, dict]) -> dict:
    """
    If payload contains a "voice" key, look it up in the pool and merge the
    resolved fields in (without overwriting anything the job already set
    explicitly). Returns a new dict; "voice" is never sent to the API.
    """
    if "voice" not in payload:
        return payload

    payload = dict(payload)
    alias = payload.pop("voice")

    if alias not in pool:
        raise KeyError(
            f"voice alias '{alias}' not found among the bundled Edge TTS voices "
            "or config.yaml's voices: section. Run `batch --list-voices` to browse, "
            "or `batch --list-voices <filter>` to search."
        )

    for field, value in pool[alias].items():
        payload.setdefault(field, value)

    return payload
