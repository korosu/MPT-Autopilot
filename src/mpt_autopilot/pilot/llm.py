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
_MIN_TOKENS = 8000  # floor — keeps small requests unchanged
_TOKENS_PER_JOB = 350  # generous per-job estimate incl. JSON overhead
_TOKENS_OVERHEAD = 500  # slack for any preamble/formatting

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
    if settings.reasoning_enabled is True:
        # Additive, not a replacement: a reasoning model spends tokens on a
        # hidden "reasoning_content" draft before it ever writes the visible
        # `content`. If we just swapped in a flat reasoning_max_tokens here
        # instead of adding it on top, we'd reintroduce the exact bug
        # _token_budget was added to fix — a big --count truncating output —
        # just for reasoning models specifically.
        max_tokens += settings.reasoning_max_tokens
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
            wait = _RETRY_BACKOFF_BASE * (2**attempt)
            print(
                f"  [retry] LLM request failed ({last_exc}); "
                f"retrying in {wait:.0f}s ({attempt + 1}/{_MAX_RETRIES})..."
            )
            time.sleep(wait)

    if resp is not None:
        return resp
    # only reached if every attempt raised a connection/timeout error
    raise last_exc or RuntimeError("LLM request failed with unknown error")


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
    data = resp.json()
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    raise ValueError(
        f"Anthropic response contained no text block (stop_reason="
        f"{data.get('stop_reason')!r}). If a 'thinking' budget is configured "
        "upstream and ate the whole max_tokens, raise max_tokens or lower "
        "the thinking budget."
    )


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

    # chat_template_kwargs.reasoning_effort is a vLLM/SGLang/NVIDIA-NIM
    # extension, NOT part of the official OpenAI API — it only means
    # anything for a self-hosted backend that reads it. Only touch the
    # payload when the user has explicitly opted in or out via
    # config.yaml (reasoning_enabled=None → key omitted entirely →
    # byte-for-byte previous behavior for every provider that doesn't
    # need this, e.g. plain OpenAI/Groq/Together/Ollama endpoints).
    # If you're on an official OpenAI reasoning model (o-series / gpt-5)
    # instead, that provider takes a top-level `reasoning_effort` field,
    # not chat_template_kwargs — check your provider's docs before relying
    # on this flag there.
    if s.reasoning_enabled is True:
        payload["chat_template_kwargs"] = {"reasoning_effort": s.reasoning_effort}
    elif s.reasoning_enabled is False:
        payload["chat_template_kwargs"] = {"reasoning_effort": "none"}

    resp = _post_with_retry(url, payload, headers)
    resp.raise_for_status()
    data = resp.json()
    message = data["choices"][0]["message"]
    content = message.get("content")

    if content is None:
        # The exact failure this replaces: `.strip()`/`["content"]` on a
        # bare None blows up as a confusing AttributeError/KeyError deep in
        # the caller. Most commonly hit when a reasoning model burns the
        # whole max_tokens budget on `reasoning_content` and gets cut off
        # (finish_reason="length") before writing the visible answer.
        finish_reason = data["choices"][0].get("finish_reason")
        reasoning_raw = message.get("reasoning_content") or message.get("reasoning") or ""
        reasoning_preview = reasoning_raw[:500]
        print(
            f"  [error] LLM returned empty content (finish_reason={finish_reason}); "
            f"reasoning preview: {reasoning_preview!r}"
        )
        raise ValueError(
            f"LLM returned empty content (finish_reason={finish_reason}). "
            "If this model has reasoning/thinking turned on by default, set "
            "generation.reasoning_enabled: true in config.yaml (and raise "
            "reasoning_max_tokens and/or lower reasoning_effort) so the "
            "reasoning draft doesn't eat the whole token budget."
        )

    return content


def parse_json_array(raw_text: str) -> list[dict[str, Any]]:
    """
    Parse the LLM response as a JSON array.
    Strips markdown fences if the model added them despite instructions.
    Falls back to extracting the outermost [ ... ] if wrapped in prose.
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
                f"LLM returned invalid JSON (no array found)\nFirst 500 chars:\n{text[:500]}"
            )
        result = json.loads(text[start : end + 1])

    if not isinstance(result, list):
        raise ValueError(f"Expected a JSON array, got {type(result).__name__}")

    return result
