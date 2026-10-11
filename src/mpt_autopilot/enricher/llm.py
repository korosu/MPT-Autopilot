"""
llm.py — all communication with the LLM API.

Public functions:
  detect_language(text)                         → str    e.g. "English"
  generate_hashtags(topic, lang, platform)      → list[str]    e.g. ["#ostrichfacts", ...]
  detect_and_generate(topic, platform)          → tuple[str, list[str]]  (language, tags)
  batch_generate_hashtags(topics, platform)     → list[dict]  per-video results in one call

`platform` is optional on generate_hashtags()/detect_and_generate(): it defaults
to settings.platform (config.yaml), but callers pass it explicitly whenever
--platform overrides the config for a run (see run.py) — this is what makes
the {platform} prompt placeholder and the platform hard-limit actually track
--platform instead of silently generating under the config.yaml platform.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from mpt_autopilot._http_utils import get_shared_client, retrying_request
from mpt_autopilot.enricher.postprocess import (
    check_platform_limit,
    platform_hard_limit,
    validate_and_filter,
)
from mpt_autopilot.enricher.reader import sanitise_topic
from mpt_autopilot.enricher.settings import settings
from mpt_autopilot.logger import Logger

# Retry settings — kept here because callers still reference _MAX_RETRIES
# for logging/degradation decisions. The actual retry loop lives in
# retrying_request() in _http_utils.py.
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 10.0
_RETRY_MAX_DELAY = 20.0

# Status codes treated as transient infra blips rather than permanent errors —
# retried with the same backoff as 429. 404 is included because NVIDIA's
# integrate.api.nvidia.com endpoint has been observed to return empty-body
# 404s under load for a URL/model that succeeds on adjacent calls.
_TRANSIENT_STATUS_CODES = frozenset({404, 500, 502, 503, 504})

# Default max_tokens for non-reasoning requests. When reasoning_enabled: true,
# this is overridden per-request by settings.reasoning_max_tokens instead,
# since hidden reasoning tokens are drawn from the same budget as the answer.
_DEFAULT_MAX_TOKENS = 512

# ── Lazy logger ───────────────────────────────────────────────────────────────
_log: Logger | None = None


def _get_log() -> Logger:
    global _log
    if _log is None:
        _log = Logger(settings.log_file, settings.max_log_size, name="mpt_autopilot.enricher")
    return _log


def _check_https(url: str) -> None:
    """
    Runtime defense-in-depth: warn if an LLM endpoint uses plain HTTP
    with a non-loopback address. Complements the config-load-time
    warn_cleartext() by catching runtime overrides or env substitutions.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or parsed.scheme != "http":
        return
    if host == "localhost" or host.endswith(".localhost"):
        return
    if host == "0.0.0.0" or host.startswith("127.") or host == "::1":
        return
    _get_log().warning(
        "LLM endpoint uses plain HTTP (not HTTPS): %s — "
        "API key and prompts travel unencrypted. Use https:// unless "
        "this is localhost.",
        url,
    )


# ── Core HTTP helper ──────────────────────────────────────────────────────────


def _chat(prompt: str) -> str:
    """
    Send a single-turn chat request via the shared HTTP client.

    Retries automatically (exponential backoff, up to _MAX_RETRIES) on:
      - 429 Too Many Requests — respects the Retry-After header when sent.
      - Transient status codes (404/500/502/503/504, see _TRANSIENT_STATUS_CODES)
        — some gateways return these intermittently under load for a request
        that otherwise succeeds.
      - Network-level failures (read/connect timeouts, connection errors).
    """
    _check_https(settings.base_url)
    url = f"{settings.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": settings.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": _DEFAULT_MAX_TOKENS,
    }

    if settings.supports_temperature:
        payload["temperature"] = 0.3

    if settings.reasoning_enabled is True:
        payload["max_tokens"] = settings.reasoning_max_tokens
        payload["chat_template_kwargs"] = {"reasoning_effort": settings.reasoning_effort}
    elif settings.reasoning_enabled is False:
        payload["chat_template_kwargs"] = {"reasoning_effort": "none"}

    response = retrying_request(
        get_shared_client(),
        "POST",
        url,
        json=payload,
        headers=headers,
        max_retries=_MAX_RETRIES,
        backoff_base=_RETRY_BASE_DELAY,
        backoff_max=_RETRY_MAX_DELAY,
        transient_status=_TRANSIENT_STATUS_CODES,
        respect_retry_after=True,
        log=lambda msg, *a: _get_log().warn(msg, *a),
    )
    data = response.json()
    # A 200 with no choices (truncated, gateway-mangled, or an error body with
    # a success status) must fail with a named error, not a bare KeyError /
    # IndexError from the middle of parsing (M-38).
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        body_preview = str(data)[:300].replace("\n", " ")
        _get_log().error("LLM response missing choices[0].message.content: %s", body_preview)
        raise ValueError(
            f"LLM response did not contain choices[0].message.content "
            f"(response preview: {body_preview})"
        ) from exc
    if not content:
        finish_reason = choice.get("finish_reason")
        reasoning_preview = (message.get("reasoning_content") or "")[:500].replace("\n", " ")
        _get_log().error(
            f"Empty content (finish_reason={finish_reason}) — "
            f"reasoning_content preview: {reasoning_preview}"
        )
        raise ValueError(
            f"Model returned empty content (finish_reason={finish_reason}). "
            f"If reasoning_enabled: true, try raising reasoning_max_tokens or "
            f"lowering reasoning_effort in config.yaml."
        )
    return content.strip()


# ── Public API ────────────────────────────────────────────────────────────────


def detect_language(text: str) -> str:
    """
    Ask the LLM what language the given text is in.
    Returns a language name string, e.g. "English", "Spanish".
    Falls back to "English" on any error.
    """
    safe_text = sanitise_topic(text)
    prompt = settings.prompt_detect_language.format(text=safe_text)
    try:
        result = _chat(prompt)
        language = result.splitlines()[0].strip().rstrip(".")
        return language if language else "English"
    except Exception:
        return "English"


def generate_hashtags(topic: str, language: str, platform: str | None = None) -> list[str]:
    """Ask the LLM to generate hashtags for the given topic."""
    safe_topic = sanitise_topic(topic)
    effective_platform = platform or settings.platform
    excluded = _build_excluded_string()

    prompt = settings.prompt_generate.format(
        video_subject=safe_topic,
        language=language,
        platform=effective_platform,
        min_tags=settings.min_tags,
        max_tags=settings.max_tags,
        max_tag_length=settings.max_tag_length,
        excluded_tags=excluded,
    )

    raw = _chat(prompt)
    return _process_raw_tags(raw, platform=effective_platform)


def detect_and_generate(topic: str, platform: str | None = None) -> tuple[str, list[str]]:
    """
    Detect the language AND generate hashtags in a SINGLE API call.
    Preferred when language is not known in advance.
    """
    safe_topic = sanitise_topic(topic)
    effective_platform = platform or settings.platform
    excluded = _build_excluded_string()

    combined_prompt = settings.prompt_detect_and_generate.format(
        video_subject=safe_topic,
        platform=effective_platform,
        min_tags=settings.min_tags,
        max_tags=settings.max_tags,
        max_tag_length=settings.max_tag_length,
        excluded_tags=excluded,
    )

    try:
        raw = _chat(combined_prompt)
        language, tags = _parse_combined_response(raw)
        tags = _finalize_tags(tags, platform=effective_platform)
        return language, tags
    except Exception as exc:
        _get_log().warn(f"detect_and_generate() combined call failed ({exc}), retrying once...")
        try:
            retry_prompt = (
                combined_prompt.rstrip() + " (IMPORTANT: respond with ONLY a JSON object "
                "containing exactly 'language' and 'tags' keys — no extra text.)"
            )
            raw = _chat(retry_prompt)
            language, tags = _parse_combined_response(raw)
            tags = _finalize_tags(tags, platform=effective_platform)
            return language, tags
        except Exception as retry_exc:
            _get_log().warn(
                f"detect_and_generate() retry also failed ({retry_exc}), "
                "falling back to two-call mode"
            )
            language = detect_language(safe_topic)
            tags = generate_hashtags(safe_topic, language, platform=effective_platform)
            return language, tags


def batch_generate_hashtags(
    topics: list[tuple[str, str]],
    platform: str,
    *,
    lang_override: str | None = None,
) -> list[dict]:
    """
    Generate hashtags for multiple videos in a single LLM call.

    Args:
        topics:   List of (filename, topic_text) pairs.
        platform: Target platform name.
        lang_override: The `--lang` flag. The batch prompt used to ignore it
            and always ask the model to detect each video's language, so the
            documented "force a specific language for all files" only worked
            with --batch-size 1 (M-34).

    Returns:
        List of dicts with keys: filename, language, tags.
        Each input entry must have exactly one result. Tags go through the
        same _finalize_tags filters as the per-file path (M-33), so the
        configured max_tags/banned/dedup rules apply in batch mode too.

    Raises:
        ValueError: if the LLM response is structurally invalid.
    """
    if not topics:
        return []

    excluded = _build_excluded_string()
    video_lines = "\n".join(
        f"{i + 1}. {_quote_fn(fn)} — topic: {sanitise_topic(topic)}"
        for i, (fn, topic) in enumerate(topics)
    )

    # {language} is either the forced --lang value or an instruction to detect
    # per video — both read naturally inside the configured prompt template,
    # so one template serves both paths (B-10/R9.1).
    language_phrase = lang_override or "the language of each video (you detect it)"
    prompt = settings.prompt_batch.format(
        count=len(topics),
        videos=video_lines,
        platform=platform,
        language=language_phrase,
        min_tags=settings.min_tags,
        max_tags=settings.max_tags,
        max_tag_length=settings.max_tag_length,
        excluded_tags=excluded,
    )

    raw = _chat(prompt)
    results = _parse_batch_response(raw, [filename for filename, _topic in topics])
    # Same quality budget as the per-file path: the prompt's max_tags is only
    # text the model may ignore, the enforced limit is here (M-33).
    for entry in results:
        entry["tags"] = _finalize_tags(entry["tags"], platform=platform)
    return results


def generate_title_description(
    topic: str,
    script: str,
    language: str,
    platform: str,
) -> dict[str, str]:
    """
    Ask the LLM for a clickable title and a short description (A-4/R2).

    The sidecar's top level carries title/description for the uploader, which
    until now only ever received a filename-derived lowercase slug and an
    empty description. Both caps match the uploader's own constants, enforced
    here so a chatty model cannot push the payload over YouTube's limits.
    """
    prompt = settings.prompt_title_description.format(
        video_subject=sanitise_topic(topic),
        script=script,
        platform=platform,
        language=language,
    )
    raw = _chat(prompt)
    title, description = _parse_title_description(raw)
    return {"title": title[:100].strip(), "description": description[:5000].strip()}


def _parse_title_description(raw: str) -> tuple[str, str]:
    """
    Parse {"title": "...", "description": "..."} — fences tolerated, anything
    else is a named error the per-video guard in run.py can show.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Title/description response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Title/description response was {type(parsed).__name__}, expected a JSON object"
        )
    title = str(parsed.get("title", "")).strip()
    description = str(parsed.get("description", "")).strip()
    if not title:
        raise ValueError("Title/description response had an empty 'title'")
    return title, description


def _quote_fn(fn: str) -> str:
    """Wrap filename in quotes for the batch prompt."""
    return f'"{fn}"'


# ── Internal helpers ──────────────────────────────────────────────────────────


def _parse_batch_response(raw: str, expected_filenames: list[str]) -> list[dict]:
    """
    Parse the LLM batch response and validate structure.

    Returns a list of {"filename": str, "language": str, "tags": list[str]}
    **in the order of expected_filenames**, not the order the model answered
    in. The caller writes each result to a sidecar, so a reordered response
    would otherwise write one video's hashtags into another's file — the
    reason the prompt asks for a filename per entry at all.

    A wrong result count is also raised here (M-35): run.py catches it and
    redoes the chunk per file, so only the failed request's tokens are lost,
    never the data.

    Raises ValueError if the response is structurally invalid or does not
    cover exactly the requested filenames.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Batch LLM returned non-JSON response: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"Batch LLM returned {type(parsed).__name__} instead of a JSON object")

    results = parsed.get("results")
    if not isinstance(results, list):
        raise ValueError(f"Batch LLM response has no 'results' list (got {type(results).__name__})")

    if len(results) != len(expected_filenames):
        raise ValueError(
            f"Batch LLM returned {len(results)} results, expected {len(expected_filenames)}"
        )

    by_filename: dict[str, dict] = {}
    for entry in results:
        if not isinstance(entry, dict):
            raise ValueError(f"Batch entry is {type(entry).__name__}, expected object")
        filename = entry.get("filename")
        language = entry.get("language", "English")
        raw_tags = entry.get("tags", [])
        if not isinstance(filename, str) or not filename:
            raise ValueError("Batch entry missing or empty 'filename'")
        if not isinstance(language, str):
            language = "English"
        if not isinstance(raw_tags, list):
            raw_tags = []
        by_filename[filename] = {
            "filename": filename,
            "language": language,
            "tags": _clean_tag_list(raw_tags),
        }

    missing = [name for name in expected_filenames if name not in by_filename]
    if missing:
        raise ValueError(
            f"Batch LLM response does not cover {len(missing)} requested "
            f"video(s), first missing: {missing[0]!r}"
        )

    return [by_filename[name] for name in expected_filenames]


def _build_excluded_string() -> str:
    """Build the comma-separated excluded-tags string for prompt injection."""
    excluded_set = list(settings.always_include) + list(settings.banned_tags)
    return ", ".join(excluded_set)


def _process_raw_tags(raw: str, platform: str | None = None) -> list[str]:
    """Parse raw LLM output and apply all post-processing filters."""
    tags = _parse_tags(raw)
    return _finalize_tags(tags, platform=platform)


def _finalize_tags(tags: list[str], platform: str | None = None) -> list[str]:
    """
    Apply post-processing filters and merge always_include tags.

    Steps:
      1. validate_and_filter — length, banned, dedup (with room reserved for
         always_include)
      2. Strip any always_include tags the LLM added anyway
      3. Prepend always_include in order

    settings.max_tags is the enforced cap here, not just prompt text (M-33):
    validate_tag_budget already guaranteed max_tags + always_include fits the
    platform hard limit at startup (and run.py re-checks for --platform), so
    min(max_tags, hard_limit - always_include) is the true content budget.
    """
    effective_platform = platform or settings.platform
    effective_hard_limit = platform_hard_limit(effective_platform)
    content_tag_limit = min(
        settings.max_tags,
        max(effective_hard_limit - len(settings.always_include), 0),
    )

    filtered = validate_and_filter(
        tags,
        max_tag_length=settings.max_tag_length,
        banned_tags=settings.banned_tags,
        hard_limit=content_tag_limit,
    )

    if len(filtered) < settings.min_tags:
        # Not fatal — the run continues with what the model produced — but
        # the shortcoming must be visible, not silently written out (M-33).
        _get_log().warn(
            "LLM produced %d usable tag(s), below enricher.min_tags=%d",
            len(filtered),
            settings.min_tags,
        )

    excluded_lower = {t.lower() for t in settings.always_include}
    filtered = [t for t in filtered if t.lower() not in excluded_lower]

    merged = _merge_always_include(filtered, settings.always_include)

    is_safe, warning = check_platform_limit(len(merged), effective_platform)
    if not is_safe:
        _get_log().warn(warning)

    return merged


def _parse_tags(raw: str) -> list[str]:
    """
    Parse the LLM response into a list of hashtag strings.

    Handles (in priority order):
      1. Clean JSON array:    ["#shorts", "#history"]
      2. Markdown fences:     ```json\\n["#shorts"]\\n```
      3. JSON object:         {"tags": ["#shorts", "#history"]}
      4. Regex fallback:      extract #word tokens from free-form text
      5. Last resort:         return []
    """
    text = raw.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(text)

        if isinstance(parsed, list):
            return _clean_tag_list(parsed)

        if isinstance(parsed, dict):
            tag_list = parsed.get("tags", [])
            if isinstance(tag_list, list):
                return _clean_tag_list(tag_list)

    except (json.JSONDecodeError, ValueError):
        pass

    candidates = re.findall(r"#\w+", raw, re.UNICODE)
    if candidates:
        return [c.lower() for c in candidates if len(c) > 2]

    return []


def _parse_combined_response(raw: str) -> tuple[str, list[str]]:
    """
    Parse the combined detect+generate JSON response.

    Expected format: {"language": "English", "tags": ["#tag1", "#tag2"]}
    """
    text = raw.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            language = str(parsed.get("language", "English")).strip().rstrip(".")
            tag_list = parsed.get("tags", [])
            if isinstance(tag_list, list):
                tags = _clean_tag_list(tag_list)
                return language or "English", tags
    except (json.JSONDecodeError, ValueError):
        pass

    lang_match = re.search(r'"language"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
    language = lang_match.group(1).strip() if lang_match else "English"
    tags = _parse_tags(raw)
    return language, tags


def _clean_tag_list(items: list) -> list[str]:
    """Normalise a list of raw strings into clean lowercase hashtags."""
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if not tag.startswith("#"):
            tag = "#" + tag
        tag = tag.replace(" ", "").lower()
        if len(tag) > 2:
            result.append(tag)
    return result


def _merge_always_include(tags: list[str], always: list[str]) -> list[str]:
    """Prepend always_include tags in order, without duplicates."""
    seen: set[str] = set()
    result: list[str] = []

    for tag in always:
        normalised = tag.lower()
        if normalised not in seen:
            seen.add(normalised)
            result.append(normalised)

    for tag in tags:
        normalised = tag.lower()
        if normalised not in seen:
            seen.add(normalised)
            result.append(tag)

    return result
