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

# A fixed budget of 8000 tokens was previously used regardless of how many
# jobs were requested. Each job is ~200-500 chars of video_subject plus ~10
# other fields plus JSON overhead — comfortably 250-350 tokens once you
# include the surrounding braces/quotes/keys. At 8000 fixed tokens, any
# --count above roughly 25-30 risks the model's output getting cut off
# mid-array (the documented `--count 50` example was a reliable repro).
# Budget now scales with the requested count instead.
_MIN_TOKENS = 8000            # floor — keeps small requests unchanged
_TOKENS_PER_JOB = 350         # generous per-job estimate incl. JSON overhead
_TOKENS_OVERHEAD = 500        # slack for any preamble/formatting

# Retry/backoff for transient failures (connection errors, timeouts, 429, 5xx).
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds; doubles each attempt


def _token_budget(count: int) -> int:
    return max(_MIN_TOKENS, count * _TOKENS_PER_JOB + _TOKENS_OVERHEAD)


def call_llm(system: str, user: str, settings: Settings, count: int) -> str:
    """
    Send a system + user prompt to the configured LLM.
    `count` is the number of job objects being requested — used to size
    max_tokens so a larger --count doesn't get silently truncated.
    Returns the raw text response.
    Raises requests.HTTPError on non-2xx responses.
    """
    max_tokens = _token_budget(count)
    if settings.is_anthropic:
        return _call_anthropic(system, user, settings, max_tokens)
    return _call_openai_compat(system, user, settings, max_tokens)


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


def _call_anthropic(system: str, user: str, s: Settings, max_tokens: int) -> str:
    url = f"{_anthropic_base(s.base_url)}/v1/messages"
    headers = {
        "x-api-key": s.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": s.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = _post_with_retry(url, payload, headers)
    resp.raise_for_status()
    for block in resp.json().get("content", []):
        if block.get("type") == "text":
            return block["text"]
    raise ValueError("Anthropic response contained no text block")


def _call_openai_compat(system: str, user: str, s: Settings, max_tokens: int) -> str:
    url = f"{s.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {s.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": s.model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = _post_with_retry(url, payload, headers)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _salvage_truncated_array(text: str) -> list[dict[str, Any]] | None:
    """
    Best-effort recovery when the response was cut off mid-array (e.g. hit
    max_tokens before finishing). Scans from the first '[' and tracks
    string/escape state and object-brace depth to find every top-level
    object that closed cleanly, then re-parses just that prefix as a
    complete array. Returns None if nothing usable is found.
    """
    start = text.find("[")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    last_complete_end = None

    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_complete_end = i + 1  # just past this object's '}'

    if last_complete_end is None:
        return None

    candidate = text[start:last_complete_end] + "]"
    try:
        result = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    return result if isinstance(result, list) else None


def parse_json_array(raw_text: str) -> list[dict[str, Any]]:
    """
    Parse the LLM response as a JSON array.
    Strips markdown fences if the model added them despite instructions.
    Falls back to extracting the outermost [ ... ] if the model wrapped
    the array in prose despite instructions not to.
    If the array itself is truncated (e.g. the response hit max_tokens
    before finishing), salvages whichever leading objects closed cleanly
    instead of failing the entire batch.
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
        result = None
        if start != -1 and end != -1 and end > start:
            try:
                result = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                result = None

        if result is None:
            salvaged = _salvage_truncated_array(text)
            if salvaged:
                print(
                    f"  [warn] LLM response looked truncated — recovered "
                    f"{len(salvaged)} complete job(s) from it; consider "
                    f"lowering --count or the response was cut short."
                )
                result = salvaged
            else:
                raise ValueError(
                    f"LLM returned invalid JSON (no array found)\n"
                    f"First 500 chars:\n{text[:500]}"
                )

    if not isinstance(result, list):
        raise ValueError(f"Expected a JSON array, got {type(result).__name__}")

    return result