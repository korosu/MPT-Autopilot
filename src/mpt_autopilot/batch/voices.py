"""
engine/voices.py

Optional convenience layer on top of MoneyPrinterTurbo's voice fields.

Engine selection happens via two fields: `tts_server` (e.g. "gtts", "gemini")
and `voice_name`, which itself encodes the provider as a "provider:voice"
prefix (e.g. "gemini:puck"). Each provider names its voices differently, so
remembering both fields by hand for every job gets old fast.

The `voices:` section of config.yaml lets you define short aliases instead,
each resolving to BOTH fields together so they can never drift out of sync:

    voices:
      gemini:
        gemini_puck:
          tts_server: "gemini"
          voice_name: "gemini:puck"

Then in jobs.yaml:

    defaults:
      voice: "gemini_puck"

config.yaml.example ships with a working set of presets already filled in,
so this works out of the box — you only need to touch the `voices:` section
if you want to add, rename, or change one.

This module only resolves the alias to the underlying field(s) — it has no
opinion about which provider is "best" and does not validate that the
provider is actually configured on your MoneyPrinterTurbo server (paid
providers need their own credentials there, see README).
"""

from __future__ import annotations

# Fields a voice preset is allowed to set on the API payload.
_ALLOWED_FIELDS = {"tts_server", "voice_name", "voice_rate", "voice_volume"}


def build_pool(voices_cfg: dict) -> dict[str, dict]:
    """
    Flatten config.yaml's `voices:` section into a single {alias: {tts_server,
    voice_name, ...}} pool. The top-level provider keys (gtts, gemini, ...)
    exist only for readability — aliases must be unique across all of them.
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
            f"voice alias '{alias}' not found in config.yaml's voices: section. "
            f"Known aliases: {sorted(pool) or '(none defined)'}"
        )

    for field, value in pool[alias].items():
        payload.setdefault(field, value)

    return payload
