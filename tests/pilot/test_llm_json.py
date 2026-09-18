"""Regression tests for malformed LLM job responses."""

from __future__ import annotations

import pytest

from mpt_autopilot.pilot.llm import parse_json_array


def test_parse_json_array_extracts_jobs_after_reasoning_prose():
    raw = """We need to output a JSON array first.
[{"output_file": "blue_whale_en.mp4", "video_subject": "Blue whale facts"}]
Done."""

    assert parse_json_array(raw) == [
        {"output_file": "blue_whale_en.mp4", "video_subject": "Blue whale facts"}
    ]


def test_parse_json_array_skips_non_job_array_in_reasoning_prose():
    raw = """Reasoning checklist: [{"step": "choose topic"}]
[{"output_file": "blue_whale_en.mp4", "video_subject": "Blue whale facts"}]"""

    assert parse_json_array(raw) == [
        {"output_file": "blue_whale_en.mp4", "video_subject": "Blue whale facts"}
    ]


def test_parse_json_array_keeps_rejecting_truncated_json_with_location():
    with pytest.raises(ValueError) as caught:
        parse_json_array('[{"output_file": "blue_whale_en.mp4"')

    message = str(caught.value)
    assert "JSONDecodeError:" in message
    assert "line 1, column" in message
    assert "Last 250 chars:" in message
