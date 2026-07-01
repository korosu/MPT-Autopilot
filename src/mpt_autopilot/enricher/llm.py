"""
llm.py — all communication with the LLM API.

Public functions:
  detect_language(text)           → str          e.g. "English"
  generate_hashtags(topic, lang)  → list[str]    e.g. ["#ostrichfacts", ...]
  detect_and_generate(topic)      → tuple[str, list[str]]  (language, tags)
                                    Single-call variant — preferred when the
                                    language isn't already known. If the caller
                                    already knows the language, call
                                    generate_hashtags() directly instead.
"""

from __future__ import annotations

import json
import re
import time

import httpx

from hashtag_enricher.enricher.config import settings
from hashtag_enricher.enricher.logger import Logger
from hashtag_enricher.enricher.postprocess import check_platform_limit, validate_and_filter

# ── Shared persistent client ──────────────────────────────────────────────────
# Reused across all calls to avoid per-call TLS handshakes.
_CLIENT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_client = httpx.Client(timeout=_CLIENT_TIMEOUT)

# ── Retry settings ────────────────────────────────────────────────────────────
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 10.0
_RETRY_MAX_DELAY = 20.0

# ── Input limits ──────────────────────────────────────────────────────────────
_MAX_TOPIC_LEN = 300

# ── Lazy logger ───────────────────────────────────────────────────────────────
_log: Logger | None = None


def _get_log() -> Logger:
    global _log
    if _log is None:
        _log = Logger(settings.log_file, settings.max_log_size)
    return _log


def _sanitise_topic(raw: str) -> str:
    """Truncate and strip non-printable characters from a user-supplied topic."""
    cleaned = "".join(ch for ch in raw if ch.isprintable())
    return cleaned[:_MAX_TOPIC_LEN]


# ── Core HTTP helper ──────────────────────────────────────────────────────────


def _chat(prompt: str) -> str:
    """
    Send a single-turn chat request. Returns the raw text content.

    Retries automatically on 429 Too Many Requests with exponential backoff.
    Respects the Retry-After header when the server sends one.
    """
    url = f"{settings.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": settings.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
    }

    if settings.supports_temperature:
        payload["temperature"] = 0.3

    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        response = _client.post(url, headers=headers, json=payload)

        if response.status_code == 429:
            if attempt == _MAX_RETRIES:
                last_error = httpx.HTTPStatusError(
                    f"429 Too Many Requests — giving up after {_MAX_RETRIES} retries",
                    request=response.request,
                    response=response,
                )
                break

            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = min(float(retry_after), _RETRY_MAX_DELAY)
            else:
                delay = min(_RETRY_BASE_DELAY * (2**attempt), _RETRY_MAX_DELAY)

            _get_log().warn(
                f"429 rate-limited — waiting {delay:.0f}s before retry {attempt + 1}/{_MAX_RETRIES}"
            )
            time.sleep(delay)
            continue

        if not response.is_success:
            # Omit body to prevent API key leakage via provider error responses.
            raise httpx.HTTPStatusError(
                f"{response.status_code} {response.reason_phrase}",
                request=response.request,
                response=response,
            )

        data = response.json()

        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as exc:
            raise ValueError(f"Unexpected API response structure: {data}") from exc

    raise last_error  # type: ignore[misc]


# ── Public API ────────────────────────────────────────────────────────────────


def detect_language(text: str) -> str:
    """
    Ask the LLM what language the given text is in.
    Returns a language name string, e.g. "English", "Spanish".
    Falls back to "English" on any error.

    Note: prefer detect_and_generate() when you also need tags — it saves one
    API round-trip by detecting language and generating tags in a single call.
    """
    safe_text = _sanitise_topic(text)
    prompt = settings.prompt_detect_language.format(text=safe_text)
    try:
        result = _chat(prompt)
        # Keep only the first word/line to guard against verbose LLM answers.
        language = result.splitlines()[0].strip().rstrip(".")
        return language if language else "English"
    except Exception:
        return "English"


def generate_hashtags(topic: str, language: str) -> list[str]:
    """
    Ask the LLM to generate hashtags for the given topic in the given language.

    Returns a list of hashtag strings starting with '#'.
    On parse failure returns a minimal fallback list so the caller never crashes.

    Note: prefer detect_and_generate() when language detection is also needed —
    it saves one API round-trip.
    """
    safe_topic = _sanitise_topic(topic)

    # Tell the LLM which tags to skip so it never generates them.
    excluded = _build_excluded_string()

    prompt = settings.prompt_generate.format(
        video_subject=safe_topic,
        language=language,
        platform=settings.platform,
        min_tags=settings.min_tags,
        max_tags=settings.max_tags,
        max_tag_length=settings.max_tag_length,
        excluded_tags=excluded,
    )

    raw = _chat(prompt)
    return _process_raw_tags(raw)


def detect_and_generate(topic: str) -> tuple[str, list[str]]:
    """
    Detect the language of the topic AND generate hashtags in a SINGLE API call.

    This is the preferred function when language is not known in advance — it
    saves one LLM round-trip compared to calling detect_language() separately.

    Note: if the language is already known (e.g. via --lang or script.json's
    video_language), the caller should call generate_hashtags() directly
    instead — this function is only for the auto-detect path.

    Args:
        topic:  The video topic/subject string.

    Returns:
        (language: str, tags: list[str])
    """
    safe_topic = _sanitise_topic(topic)
    excluded = _build_excluded_string()

    # Combined detect+generate prompt — fully defined in config.yaml as
    # prompt_detect_and_generate, the same way prompt_generate is.
    combined_prompt = settings.prompt_detect_and_generate.format(
        video_subject=safe_topic,
        platform=settings.platform,
        min_tags=settings.min_tags,
        max_tags=settings.max_tags,
        max_tag_length=settings.max_tag_length,
        excluded_tags=excluded,
    )

    try:
        raw = _chat(combined_prompt)
        language, tags = _parse_combined_response(raw)
        tags = _finalize_tags(tags)
        return language, tags
    except Exception as exc:
        _get_log().warn(f"detect_and_generate() failed ({exc}), falling back to two-call mode")
        # Graceful fallback: two separate calls
        language = detect_language(safe_topic)
        tags = generate_hashtags(safe_topic, language)
        return language, tags


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_excluded_string() -> str:
    """Build the comma-separated excluded-tags string for prompt injection."""
    excluded_set = list(settings.always_include) + list(settings.banned_tags)
    return ", ".join(excluded_set)


def _process_raw_tags(raw: str) -> list[str]:
    """Parse raw LLM output and apply all post-processing filters."""
    tags = _parse_tags(raw)
    return _finalize_tags(tags)


def _finalize_tags(tags: list[str]) -> list[str]:
    """
    Apply post-processing filters and merge always_include tags.

    Steps:
    1. validate_and_filter — length, banned, dedup, hard_limit
    2. Strip any always_include tags the LLM added anyway
    3. Prepend always_include in order
    """
    # 1. Validate and filter
    filtered = validate_and_filter(
        tags,
        max_tag_length=settings.max_tag_length,
        banned_tags=settings.banned_tags,
        hard_limit=settings.hard_limit,
    )

    # 2. Strip always_include duplicates (LLM may have added them despite instructions)
    excluded_lower = {t.lower() for t in settings.always_include}
    filtered = [t for t in filtered if t.lower() not in excluded_lower]

    # 3. Merge: always_include first, then LLM-generated content tags
    merged = _merge_always_include(filtered, settings.always_include)

    # 4. Final platform limit check (warn but don't crash)
    is_safe, warning = check_platform_limit(len(merged), settings.platform)
    if not is_safe:
        _get_log().warn(warning)

    return merged


def _parse_tags(raw: str) -> list[str]:
    """
    Parse the LLM response into a list of hashtag strings.

    Handles (in priority order):
      1. Clean JSON array:    ["#shorts", "#history"]
      2. Markdown fences:     ```json\\n["#shorts"]\\n```
      3. JSON object:         {"tags": ["#shorts", "#history"]} (combined prompt fallback)
      4. Regex fallback:      extract #word tokens from free-form text
      5. Last resort:         return []

    Post-processing:
      - Forced to lowercase (canonical form on all platforms)
      - Diacritics/accents are preserved (they matter: #recuperacion ≠ #recuperación)
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    # Try JSON parse
    try:
        parsed = json.loads(text)

        # Case A: plain JSON array  ["#tag1", "#tag2"]
        if isinstance(parsed, list):
            return _clean_tag_list(parsed)

        # Case B: JSON object {"tags": [...]} or {"language": "...", "tags": [...]}
        if isinstance(parsed, dict):
            tag_list = parsed.get("tags", [])
            if isinstance(tag_list, list):
                return _clean_tag_list(tag_list)

    except (json.JSONDecodeError, ValueError):
        pass

    # Regex fallback — works for:
    #   - Numbered lists:  1. #tag
    #   - Prose output:    "Use #tag and #other"
    #   - Any Unicode script (Unicode flag ensures \w matches all letters)
    candidates = re.findall(r"#\w+", raw, re.UNICODE)
    if candidates:
        return [c.lower() for c in candidates if len(c) > 2]

    return []


def _parse_combined_response(raw: str) -> tuple[str, list[str]]:
    """
    Parse the combined detect+generate JSON response.

    Expected format: {"language": "English", "tags": ["#tag1", "#tag2"]}

    Returns:
        (language, tags) — with fallback to "English" on error.
    """
    text = raw.strip()

    # Strip markdown fences
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

    # Fallback: try to extract language and tags separately from raw text
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
        if len(tag) > 2:  # '#' + at least 2 characters
            result.append(tag)
    return result


def _merge_always_include(tags: list[str], always: list[str]) -> list[str]:
    """
    Prepend always_include tags in order, without duplicates.
    Case-insensitive dedup.
    """
    seen: set[str] = set()
    result: list[str] = []

    for tag in always:
        normalised = tag.lower()
        if normalised not in seen:
            seen.add(normalised)
            result.append(tag)

    for tag in tags:
        normalised = tag.lower()
        if normalised not in seen:
            seen.add(normalised)
            result.append(tag)

    return result
