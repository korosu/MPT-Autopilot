"""
llm.py — all communication with the LLM API.

Two public functions:
  detect_language(text)           → str  e.g. "English"
  generate_hashtags(topic, lang)  → list[str]  e.g. ["#shorts", "#history", ...]
"""

from __future__ import annotations

import json
import re
import time

import httpx

from enricher.config import settings

# Shared persistent client — reused across all calls to avoid per-call TLS handshakes
_CLIENT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_client = httpx.Client(timeout=_CLIENT_TIMEOUT)

# Retry settings for 429 Too Many Requests
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 10.0
_RETRY_MAX_DELAY = 20.0

# Maximum characters accepted from a user-controlled topic string
_MAX_TOPIC_LEN = 300


def _sanitise_topic(raw: str) -> str:
    """Truncate and strip non-printable characters from a user-supplied topic."""
    cleaned = "".join(ch for ch in raw if ch.isprintable())
    return cleaned[:_MAX_TOPIC_LEN]


def _chat(prompt: str) -> str:
    """Send a single-turn chat request. Returns the raw text content.

    Retries automatically on 429 Too Many Requests using exponential backoff.
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

    # Use explicit config flag; avoids fragile model-name prefix sniffing
    if settings.supports_temperature:
        payload["temperature"] = 0.3

    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        response = _client.post(url, headers=headers, json=payload)

        if response.status_code == 429:
            if attempt == _MAX_RETRIES:
                # Out of retries — fall through to raise below
                last_error = httpx.HTTPStatusError(
                    f"429 Too Many Requests — giving up after {_MAX_RETRIES} retries",
                    request=response.request,
                    response=response,
                )
                break

            # Respect Retry-After if provided, otherwise use exponential backoff
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = min(float(retry_after), _RETRY_MAX_DELAY)
            else:
                delay = min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY)

            print(f"[llm] 429 rate-limited — waiting {delay:.0f}s before retry {attempt + 1}/{_MAX_RETRIES}...")
            time.sleep(delay)
            continue

        if not response.is_success:
            # Raise with status only — omit body to prevent API key leakage via
            # providers that echo request headers/auth in error responses.
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


def detect_language(text: str) -> str:
    """
    Ask the LLM what language the given text is in.
    Returns a language name string, e.g. "English", "Spanish".
    Falls back to "English" on any error.
    """
    safe_text = _sanitise_topic(text)
    prompt = settings.prompt_detect_language.format(text=safe_text)
    try:
        result = _chat(prompt)
        # Sanitise: keep only the first word/line to guard against verbose answers
        language = result.splitlines()[0].strip().rstrip(".")
        return language if language else "English"
    except Exception:
        return "English"


def generate_hashtags(topic: str, language: str) -> list[str]:
    """
    Ask the LLM to generate hashtags for the given topic in the given language.

    Returns a list of hashtag strings starting with '#'.
    On parse failure returns a minimal fallback list so the caller never crashes.
    """
    safe_topic = _sanitise_topic(topic)

    # Tell the LLM exactly which tags to skip so it never generates them itself.
    # This prevents duplicates regardless of what the user puts in always_include.
    excluded = ", ".join(settings.always_include)

    prompt = settings.prompt_generate.format(
        video_subject=safe_topic,
        language=language,
        min_tags=settings.min_tags,
        max_tags=settings.max_tags,
        excluded_tags=excluded,
    )

    raw = _chat(prompt)
    tags = _parse_tags(raw)

    # Strip any always_include tags the LLM added anyway (case-insensitive safety net)
    excluded_lower = {t.lower() for t in settings.always_include}
    tags = [t for t in tags if t.lower() not in excluded_lower]

    # Prepend always_include tags in order, then the LLM-generated content tags
    merged = _merge_always_include(tags, settings.always_include)
    return merged


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_tags(raw: str) -> list[str]:
    """
    Parse the LLM response into a list of hashtag strings.
    Handles:
      - Clean JSON array:  ["#shorts", "#history"]
      - Markdown fences:   ```json\n["#shorts"]\n```
      - Regex fallback:    extract #word tokens from free-form text
      - Last resort:       return []

    Post-processing applied to every tag regardless of LLM compliance:
      - Forced to lowercase (YouTube stores all tags lowercase; CamelCase
        like #vidaSaludable and #vidasaludable are the same tag on YouTube,
        but lowercase is the canonical form users actually search for)
      - Diacritics/accents are preserved as-is (they matter: #recuperacion
        and #recuperación are different tags with different audiences)
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            cleaned: list[str] = []
            for item in parsed:
                if isinstance(item, str):
                    tag = item.strip()
                    if not tag.startswith("#"):
                        tag = "#" + tag
                    tag = tag.replace(" ", "")
                    # Enforce lowercase — safety net even if the prompt is ignored.
                    # We lowercase only the part after '#' to be explicit,
                    # but since '#' is not a letter, tag.lower() is equivalent.
                    tag = tag.lower()
                    if tag and len(tag) > 1:
                        cleaned.append(tag)
            return cleaned
    except (json.JSONDecodeError, ValueError):
        pass

    # Regex fallback: LLM returned prose or a non-JSON list — extract #tokens directly.
    # Matches ASCII word chars plus common accented/diacritic characters (Latin Extended).
    candidates = re.findall(r"#[\w\u00C0-\u024F]+", raw)
    if candidates:
        return [c.lower() for c in candidates if len(c) > 1]

    return []


def _merge_always_include(tags: list[str], always: list[str]) -> list[str]:
    """
    Ensure always_include tags appear first (in order), without duplicates.
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
