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
import time
from typing import Any

import requests

from shorts_pilot.generator.settings import Settings

_MAX_TOKENS = 8000

# Retry/backoff for transient failures (connection errors, timeouts, 429, 5xx).
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds; doubles each attempt


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


def _post_with_retry(url: str, payload: dict, headers: dict) -> requests.Response:
    """
    POST with a small retry/backoff for transient failures: connection
    errors, timeouts, 429 (rate limit), and 5xx responses. Anything else
    (4xx client errors) is returned as-is for raise_for_status() to handle.
    """
    last_exc: Exception | None = None
    resp: requests.Response | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            resp = None
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = requests.HTTPError(f"{resp.status_code} {resp.reason}", response=resp)
            else:
                return resp

        if attempt < _MAX_RETRIES:
            wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
            print(f"  [retry] LLM request failed ({last_exc}); "
                  f"retrying in {wait:.0f}s ({attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)

    if resp is not None:
        return resp
    raise last_exc  # only reached if every attempt raised a connection/timeout error


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
    resp = _post_with_retry(url, payload, headers)
    resp.raise_for_status()
    for block in resp.json().get("content", []):
        if block.get("type") == "text":
            return block["text"]
    raise ValueError("Anthropic response contained no text block")


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
    resp = _post_with_retry(url, payload, headers)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_json_array(raw_text: str) -> list[dict[str, Any]]:
    """
    Parse the LLM response as a JSON array.
    Strips markdown fences if the model added them despite instructions.
    Falls back to extracting the outermost [ ... ] if the model wrapped
    the array in prose despite instructions not to.
    """
    text = raw_text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"LLM returned invalid JSON (no array found)\n"
                f"First 500 chars:\n{text[:500]}"
            )
        try:
            result = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(
                f"LLM returned invalid JSON: {e}\n"
                f"First 500 chars:\n{text[:500]}"
            ) from e

    if not isinstance(result, list):
        raise ValueError(f"Expected a JSON array, got {type(result).__name__}")

    return result