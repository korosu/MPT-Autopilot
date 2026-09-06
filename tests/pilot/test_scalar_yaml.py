"""
tests/pilot/test_scalar_yaml.py — pilot/jobs.py _scalar() must escape every
character that YAML double-quoted scalars require escaping.

_scalar() is called on every LLM-supplied field before it is appended to
jobs.yaml. It escapes \\, ", \r\n, \n, \r, \t — but NOT the other control
characters YAML rejects (NUL, BEL, VT, FF, ESC, DEL). A control character
therefore lands verbatim in jobs.yaml, making the file unparsable; the next
`mpt batch` / `mpt refill` then dies with a yaml.ReaderError and the jobs file
stays broken until hand-repaired.

A realistic input path: `mpt refill --topics my_topics.txt`. refill passes each
line verbatim into video_subject (only list markers are stripped) and
unlike enricher/llm.py's _sanitise_topic(), nothing removes non-printable
characters.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from mpt_autopilot.pilot import jobs

# Every control character YAML 1.1 forbids inside a double-quoted scalar.
CONTROLS = {
    "nul": "\x00",
    "bell": "\x07",
    "vt": "\x0b",
    "ff": "\x0c",
    "esc": "\x1b",
    "del": "\x7f",
}


@pytest.mark.parametrize("label,ch", sorted(CONTROLS.items(), key=lambda kv: kv[1]))
def test_control_char_round_trips(label: str, ch: str) -> None:
    """A control character in an LLM value must not corrupt jobs.yaml."""
    value = f"ab{ch}cd"
    emitted = jobs._scalar(value)
    doc = f"video_subject: {emitted}\nvideo_clip_duration: 3\n"

    parsed = yaml.safe_load(doc)
    assert parsed["video_subject"] == value, (
        f"control char {label!r} ({ch!r}) was written unescaped: {emitted!r}"
    )
    assert parsed["video_clip_duration"] == 3, "the following key was lost or injected"


def test_control_char_survives_the_full_append_path(tmp_path: Path) -> None:
    """End-to-end: jobs.append() must not leave jobs.yaml unparsable."""
    (tmp_path / "jobs.yaml").write_text("jobs:\n", encoding="utf-8")
    jobs.append(
        tmp_path,
        "",
        "en",
        [{"name": "x", "enabled": True, "output_file": "x.mp4", "video_subject": "ab\x00cd"}],
    )
    parsed = yaml.safe_load((tmp_path / "jobs.yaml").read_text(encoding="utf-8"))
    assert parsed["jobs"][0]["video_subject"] == "ab\x00cd"


def test_backslash_and_quote_are_escaped(tmp_path: Path) -> None:
    """Regression guard for the characters _scalar() already handles."""
    for value in ("trail\\", 'he said "hi"', 'mix\\ "tab\t"'):
        parsed = yaml.safe_load(f"v: {jobs._scalar(value)}\n")
        assert parsed["v"] == value, f"broken escape for {value!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
