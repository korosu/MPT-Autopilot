"""
pilot/llm.py

Single LLM client that works with any OpenAI-compatible provider
AND Anthropic natively (auto-detected by base_url).

Any service exposing a /chat/completions endpoint works out of the box:
OpenAI, Groq, Together, Mistral, Ollama, DeepSeek, Qwen, OpenRouter,
LiteLLM, vLLM, SGLang, NVIDIA NIM, and others. Set LLM_BASE_URL +
LLM_MODEL in .env and it just works.

Anthropic is detected automatically when the base_url contains
"anthropic.com" and routed to /v1/messages instead.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

import httpx

from mpt_autopilot._http_utils import get_shared_client, retrying_request
from mpt_autopilot.config import warn_cleartext
from mpt_autopilot.pilot.settings import Settings

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

# Per-request timeout (connect + read). Reasoning models (e.g. NVIDIA NIM
# inkling) spend a long time on a hidden reasoning draft before writing the
# visible answer, so a big --count can legitimately take minutes.
_REQUEST_TIMEOUT = 480  # seconds

# Status codes treated as transient infra blips — same set the previous
# _post_with_retry used (429 + 5xx).
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _token_budget(count: int) -> int:
    return max(_MIN_TOKENS, count * _TOKENS_PER_JOB + _TOKENS_OVERHEAD)


def call_llm(system: str, user: str, settings: Settings, count: int) -> str:
    """
    Send a system + user prompt to the configured LLM.
    `count` is the number of job objects being requested — used to size
    max_tokens so a larger --count doesn't get silently truncated.
    Returns the raw text response.
    Raises httpx.HTTPStatusError on non-2xx responses.
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


def _post_with_retry(
    url: str,
    payload: dict,
    headers: dict,
    *,
    timeout: int = _REQUEST_TIMEOUT,
) -> httpx.Response:
    """
    POST with retry/backoff for transient failures, delegated to the shared
    retrying_request() helper. Preserves the previous retry semantics
    (429 + 5xx, configurable backoff).
    """
    return retrying_request(
        get_shared_client(),
        "POST",
        url,
        json=payload,
        headers=headers,
        max_retries=_MAX_RETRIES,
        backoff_base=_RETRY_BACKOFF_BASE,
        transient_status=_TRANSIENT_STATUS,
        timeout=timeout,
        log=lambda msg, *a: print(f"  [retry] {msg % a if a else msg}"),
    )


def _call_anthropic(system: str, user: str, s: Settings, max_tokens: int) -> str:
    warn_cleartext(_anthropic_base(s.base_url), "LLM_BASE_URL")
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
    warn_cleartext(s.base_url, "LLM_BASE_URL")
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


def _strip_trailing_artifact(value: str) -> str:
    """Remove a stray ')' and a trailing autocomplete ',' appended by the model."""
    value = value.rstrip()
    if value.endswith(",)") or value.endswith("])"):
        value = value[:-1].rstrip()
    elif value.endswith(","):
        value = value[:-1].rstrip()
    return value


def _normalise_json_item(item: object) -> Any:
    """Recursively strip trailing artefacts from leaf strings in a parsed JSON value."""
    if isinstance(item, str):
        return _strip_trailing_artifact(item)
    if isinstance(item, dict):
        return {k: _normalise_json_item(v) for k, v in item.items()}
    if isinstance(item, list):
        return [_normalise_json_item(v) for v in item]
    return item


def parse_json_array(raw_text: str) -> list[dict[str, Any]]:
    """
    Parse the LLM response as a JSON array.

    Strips markdown fences and trailing autocomplete artefacts (a stray ``)``
    or a trailing ``,`` after the closing bracket) that some providers emit
    when the model restarts its own completion.
    """
    text = raw_text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(
            f"LLM returned invalid JSON (could not parse JSON array)\n"
            f"First 500 chars:\n{text[:500]}"
        )

    if not isinstance(result, list):
        raise ValueError(
            f"LLM returned invalid JSON (expected array, got {type(result).__name__})\n"
            f"First 500 chars:\n{text[:500]}"
        )

    return cast("list[dict[str, Any]]", [_normalise_json_item(item) for item in result])
