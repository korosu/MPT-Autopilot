"""
llm.py — all communication with the LLM API.

Two public functions:
  detect_language(text)           → str  e.g. "English"
  generate_hashtags(topic, lang)  → list[str]  e.g. ["#shorts", "#history", ...]
"""

from __future__ import annotations

import json

import httpx

from enricher.config import settings

# Shared client with a reasonable timeout
_CLIENT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Models that do not accept a 'temperature' parameter (OpenAI o-series reasoning models)
_NO_TEMPERATURE_PREFIXES = ("o1", "o3", "o4")


def _supports_temperature(model: str) -> bool:
    """Return False for reasoning models (o1, o3, o4 families) that reject temperature."""
    # Check the part after the last '/' to handle namespaced models like "openai/o4-mini"
    name = model.split("/")[-1].lower()
    return not any(name.startswith(prefix) for prefix in _NO_TEMPERATURE_PREFIXES)


def _chat(prompt: str) -> str:
    """Send a single-turn chat request. Returns the raw text content."""
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

    # Only include temperature for models that support it
    if _supports_temperature(settings.model):
        payload["temperature"] = 0.3

    response = httpx.post(url, headers=headers, json=payload, timeout=_CLIENT_TIMEOUT)

    if not response.is_success:
        # Include response body in the error to make debugging easier
        try:
            body = response.json()
        except Exception:
            body = response.text
        raise httpx.HTTPStatusError(
            f"{response.status_code} {response.reason_phrase} — {body}",
            request=response.request,
            response=response,
        )

    data = response.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected API response structure: {data}") from exc


def detect_language(text: str) -> str:
    """
    Ask the LLM what language the given text is in.
    Returns a language name string, e.g. "English", "Spanish".
    Falls back to "English" on any error.
    """
    prompt = settings.prompt_detect_language.format(text=text)
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
    prompt = settings.prompt_generate.format(
        video_subject=topic,
        language=language,
        max_tags=settings.max_tags,
    )

    raw = _chat(prompt)
    tags = _parse_tags(raw)

    # Merge with always_include (prepend, deduplicate, respect order)
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
      - Fallback:          return []

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