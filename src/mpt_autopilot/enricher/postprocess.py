"""
postprocess.py — validates and filters LLM-generated hashtags.

Applied after LLM generation, before final output.
Enforces tag-length limits, banned-tag removal, deduplication,
and platform-specific hard limits.
"""

from __future__ import annotations

import re

# ── Default constants (overridden by Settings values passed in) ───────────────

_DEFAULT_MAX_TAG_LENGTH = 20
_DEFAULT_HARD_LIMIT = 60

PLATFORM_HARD_LIMITS: dict[str, int] = {
    "youtube": 60,
    "tiktok": 5,
    "instagram": 5,
}


def validate_and_filter(
    tags: list[str],
    *,
    max_tag_length: int = _DEFAULT_MAX_TAG_LENGTH,
    banned_tags: frozenset[str] | None = None,
    hard_limit: int = _DEFAULT_HARD_LIMIT,
) -> list[str]:
    """
    Clean and validate a list of hashtag strings.

    Filters applied in order:
    1. Ensure each tag starts with '#' (defensive normalisation).
    2. Minimum length — tags that are just '#' or '#x' are dropped (< 3 chars).
    3. Max length — tags with more than max_tag_length characters after '#' are dropped.
    4. Banned list — caller-supplied tags are removed (empty by default; this
       function has no opinion of its own about which tags are "bad" — the
       caller decides via the banned_tags argument, which is sourced from the
       user's own config.yaml).
    5. Deduplication — case-insensitive, first occurrence wins.
    6. Hard limit — final list is truncated to hard_limit (platform safety net).

    Args:
        tags:           Raw list from LLM (after JSON parse).
        max_tag_length: Maximum allowed characters after '#'.
        banned_tags:    Tags to exclude, supplied by the caller. Defaults to
                         an empty set — no tags are banned unless the user
                         explicitly lists them in config.yaml's banned_tags.
        hard_limit:     Maximum number of tags to return.

    Returns:
        Cleaned, deduplicated list of valid hashtags, length ≤ hard_limit.
    """
    effective_banned = banned_tags or frozenset()
    seen: set[str] = set()
    result: list[str] = []

    for raw_tag in tags:
        tag = raw_tag.strip()

        # Ensure leading '#'
        if not tag.startswith("#"):
            tag = "#" + tag

        tag_lower = tag.lower()

        # Deduplication (case-insensitive)
        if tag_lower in seen:
            continue
        seen.add(tag_lower)

        # Minimum length: '#' + at least 2 characters
        if len(tag) < 3:
            continue

        # Max length: count only the part after '#'
        tag_body = tag[1:]
        if len(tag_body) > max_tag_length:
            continue

        # Banned list (case-insensitive, user-supplied only)
        if tag_lower in effective_banned:
            continue

        result.append(tag_lower)

    # Enforce platform hard limit (safety net — correct config should prevent this)
    return result[:hard_limit]


def check_platform_limit(
    total_tag_count: int,
    platform: str,
) -> tuple[bool, str]:
    """
    Check whether the total tag count (always_include + generated) is within
    the target platform's tag-count limit. This is a safety-net check —
    validate_and_filter() already truncates to hard_limit, so this should
    only ever fire if something upstream bypassed it.

    Returns:
        (is_safe: bool, warning_message: str)
        warning_message is empty when is_safe is True.
    """
    hard_limit = PLATFORM_HARD_LIMITS.get(platform, _DEFAULT_HARD_LIMIT)

    if total_tag_count > hard_limit:
        return False, (f"{total_tag_count} total tags exceed the {platform} limit of {hard_limit}.")

    return True, ""


def tag_has_valid_charset(tag: str) -> bool:
    """
    Return True if the tag body contains only valid hashtag characters.

    Valid: letters (any Unicode script), digits, underscores.
    Invalid: punctuation, spaces, special symbols.
    """
    body = tag.lstrip("#")
    return bool(re.match(r"^\w+$", body, re.UNICODE))
