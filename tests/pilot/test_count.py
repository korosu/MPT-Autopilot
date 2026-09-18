#!/usr/bin/env python3
"""
test_count.py — tests for --count flag behavior.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Add src to path
src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from mpt_autopilot.pilot.refill import run  # noqa: E402
from mpt_autopilot.pilot.settings import LangSettings, Settings  # noqa: E402


def make_settings(
    count: int = 21, threshold: int = 10, suffix: str = "", prefix: str = "shorts-pilot"
) -> Settings:
    """Build a minimal Settings with the given generation params."""
    return Settings(
        api_key="",
        base_url="",
        model="",
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix=prefix,
        generate_count=count,
        refill_threshold=threshold,
        scan_dirs=[],
        langs={
            "en": LangSettings(
                label="English",
                file_suffix=suffix,
                voice_rate_min=1.05,
                voice_rate_max=1.20,
                voices=["gemini:puck"],
                job_defaults={
                    "video_clip_duration": 3,
                    "video_concat_mode": "random",
                    "bgm_type": "random",
                    "bgm_volume": 0.15,
                    "paragraph_number": 2,
                },
                theme_list=[],
            )
        },
        jobs_dir=None,
        seen_dir=None,
        seen_max_mb=64,
    )


def make_jobs_yaml(path: Path, jobs_list: list[dict] | None = None) -> None:
    """Write a minimal jobs.yaml with optional pre-existing jobs."""
    content = "jobs:\n"
    if jobs_list:
        for j in jobs_list:
            name = j.get("name", j["output_file"].replace(".mp4", ""))
            content += f"  - name: {json.dumps(name)}\n"
            content += "    enabled: true\n"
            content += f"    output_file: {json.dumps(j['output_file'])}\n"
    path.write_text(content, encoding="utf-8")


def test_count_one_call_regardless_of_threshold():
    """--count N should invoke LLM exactly once, ignoring threshold top-up."""
    call_count = 0

    def mock_call_llm(system_prompt, user_prompt, settings, count):
        nonlocal call_count
        call_count += 1
        # Return exactly N jobs (N = the count passed)
        return json.dumps(
            [
                {
                    "output_file": f"fact_{call_count}_a.mp4",
                    "video_subject": "Octopuses have three hearts pumping blue blood everywhere ",
                },
                {
                    "output_file": f"fact_{call_count}_b.mp4",
                    "video_subject": "Penguins propose to mates by presenting polished volcanic stones ",
                },
                {
                    "output_file": f"fact_{call_count}_c.mp4",
                    "video_subject": "Giraffes can go weeks without drinking water at all ",
                },
            ]
        )

    # Patch both load_settings and call_llm in the refill module
    from unittest.mock import patch

    import mpt_autopilot.pilot.refill as refill

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        jobs_file = tmp_dir / "jobs.yaml"
        make_jobs_yaml(jobs_file)

        with patch.object(refill, "load_settings", return_value=make_settings(threshold=50)):
            with patch.object(refill, "call_llm", mock_call_llm):
                # Empty queue, threshold=50, ask for 3 → should get exactly one LLM call
                added = run(
                    lang="en",
                    jobs_dir=tmp_dir,
                    seen_dir=tmp_dir,
                    force=False,
                    count_override=3,
                    threshold_override=50,
                    topics=None,
                    themes=None,
                )

        assert call_count == 1, f"Expected exactly 1 LLM call, got {call_count}"
        assert added == 3, f"Expected 3 jobs added, got {added}"

    print("[check] --count one call (empty queue, threshold=50): OK")


def test_count_bypasses_full_queue_guard():
    """--count N should proceed even when queue already exceeds threshold."""
    call_count = 0

    def mock_call_llm(system_prompt, user_prompt, settings, count):
        nonlocal call_count
        call_count += 1
        return json.dumps(
            [
                {
                    "output_file": "fact_one.mp4",
                    "video_subject": "Octopuses have three hearts and blue blood circulating through their arms ",
                },
                {
                    "output_file": "fact_two.mp4",
                    "video_subject": "Penguins propose with stones that they have polished on the ocean floor ",
                },
                {
                    "output_file": "fact_three.mp4",
                    "video_subject": "Giraffes can survive without water for extremely long stretches of time ",
                },
            ]
        )

    from unittest.mock import patch

    import mpt_autopilot.pilot.refill as refill

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # Queue is "full" (15 pending, threshold=10)
        existing = [
            {
                "output_file": f"existing_{i}.mp4",
                "video_subject": f"Existing fact number {i} about nature and biology ",
            }
            for i in range(15)
        ]
        make_jobs_yaml(tmp_dir / "jobs.yaml", existing)

        with patch.object(refill, "load_settings", return_value=make_settings(threshold=10)):
            with patch.object(refill, "call_llm", mock_call_llm):
                # Guard should be bypassed; LLM still called once
                added = run(
                    lang="en",
                    jobs_dir=tmp_dir,
                    seen_dir=tmp_dir,
                    force=False,
                    count_override=3,
                    threshold_override=10,
                    topics=None,
                    themes=None,
                )

        assert call_count == 1, f"Expected exactly 1 LLM call (guard bypassed), got {call_count}"
        assert added == 3, f"Expected 3 jobs added, got {added}"

    print("[check] --count bypasses full-queue guard: OK")


def test_no_count_uses_threshold_guard():
    """Without --count, threshold guard should still work (default behavior)."""
    from unittest.mock import patch

    import mpt_autopilot.pilot.refill as refill

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # Queue is "full" (15 pending, threshold=10)
        existing = [
            {"output_file": f"existing_{i}.mp4", "video_subject": f"Existing fact number {i} " * 5}
            for i in range(15)
        ]
        make_jobs_yaml(tmp_dir / "jobs.yaml", existing)

        with patch.object(refill, "load_settings", return_value=make_settings(threshold=10)):
            # No --count (count_override=None), no --force → should skip
            added = run(
                lang="en",
                jobs_dir=tmp_dir,
                seen_dir=tmp_dir,
                force=False,
                count_override=None,
                threshold_override=10,
                topics=None,
                themes=None,
            )

        assert added == 0, f"Expected 0 jobs (guard triggered), got {added}"

    print("[check] no --count respects threshold guard: OK")


def test_refill_accepts_jobs_after_reasoning_prefix():
    """A provider that leaks reasoning prose must not discard a valid job array."""
    from unittest.mock import patch

    import mpt_autopilot.pilot.refill as refill

    raw_response = """I need to generate one fresh job first.
[{"output_file": "whale_heart.mp4", "video_subject": "Blue whale hearts are larger than cars"}]
"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        make_jobs_yaml(tmp_dir / "jobs.yaml")

        with patch.object(refill, "load_settings", return_value=make_settings(count=1)):
            with patch.object(refill, "call_llm", return_value=raw_response):
                added = run(
                    lang="en",
                    jobs_dir=tmp_dir,
                    seen_dir=tmp_dir,
                    force=False,
                    count_override=1,
                    threshold_override=None,
                    topics=None,
                    themes=None,
                )

    assert added == 1


def test_refill_splits_invalid_json_and_keeps_valid_partial_results():
    """A persistently truncated branch must not discard valid sibling jobs."""
    from unittest.mock import patch

    import mpt_autopilot.pilot.refill as refill

    calls: list[int] = []

    def mock_call_llm(system_prompt, user_prompt, settings, count):
        calls.append(count)
        if len(calls) <= 2:
            return '[{"output_file": "truncated.mp4"'
        if len(calls) == 3:
            return json.dumps(
                [
                    {
                        "output_file": "valid_one.mp4",
                        "video_subject": "Octopuses have three hearts and bright blue blood throughout their bodies",
                    },
                    {
                        "output_file": "valid_two.mp4",
                        "video_subject": "Penguins use carefully selected stones during courtship on Antarctic shores",
                    },
                ]
            )
        return '[{"output_file": "still_truncated.mp4"'

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        make_jobs_yaml(tmp_dir / "jobs.yaml")

        with patch.object(refill, "load_settings", return_value=make_settings(count=3)):
            with patch.object(refill, "call_llm", side_effect=mock_call_llm):
                added = run(
                    lang="en",
                    jobs_dir=tmp_dir,
                    seen_dir=tmp_dir,
                    force=False,
                    count_override=3,
                    threshold_override=None,
                    topics=None,
                    themes=None,
                )

        jobs_text = (tmp_dir / "jobs.yaml").read_text(encoding="utf-8")

    assert calls == [3, 3, 2, 1, 1]
    assert added == 2
    assert "valid_one.mp4" in jobs_text
    assert "valid_two.mp4" in jobs_text


def main() -> None:
    test_count_one_call_regardless_of_threshold()
    test_count_bypasses_full_queue_guard()
    test_no_count_uses_threshold_guard()
    test_refill_accepts_jobs_after_reasoning_prefix()
    test_refill_splits_invalid_json_and_keeps_valid_partial_results()
    print("test_count: OK")


if __name__ == "__main__":
    main()
