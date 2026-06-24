"""
generator/llm.py

Single LLM client that works with any OpenAI-compatible provider
AND Anthropic natively — detected automatically by LLM_BASE_URL.

Supported out of the box:
  OpenAI     → https://api.openai.com/v1
  Groq       → https://api.groq.com/openai/v1
  Together   → https://api.together.xyz/v1
  Mistral    → https://api.mistral.ai/v1
  Ollama     → http://localhost:11434/v1
  Anthropic  → https://api.anthropic.com   (auto-detected)
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from src.shorts_pilot.generator.settings import Settings

_MAX_TOKENS = 8000


def call_llm(system: str, user: str, settings: Settings) -> str:
    """
    Send a system + user prompt to the configured LLM.
    Returns the raw text response.
    Raises requests.HTTPError on non-2xx responses.
    """
    if settings.is_anthropic:
        return _call_anthropic(system, user, settings)
    return _call_openai_compat(system, user, settings)


def _anthropic_base(base_url: str) -> str:
    """Normalise Anthropic base URL — strip accidental /v1 suffix."""
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def _call_anthropic(system: str, user: str, s: Settings) -> str:
    url = f"{_anthropic_base(s.base_url)}/v1/messages"
    headers = {
        "x-api-key": s.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": s.model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def _call_openai_compat(system: str, user: str, s: Settings) -> str:
    url = f"{s.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {s.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": s.model,
        "max_tokens": _MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_json_array(raw_text: str) -> list[dict[str, Any]]:
    """
    Parse the LLM response as a JSON array.
    Strips markdown fences if the model added them despite instructions.
    """
    text = raw_text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM returned invalid JSON: {e}\n"
            f"First 500 chars:\n{text[:500]}"
        ) from e

    if not isinstance(result, list):
        raise ValueError(f"Expected a JSON array, got {type(result).__name__}")

    return result