#!/usr/bin/env python3
"""
Regenerate src/mpt_autopilot/batch/data/edge_voices.json from
MoneyPrinterTurbo's docs/voice-list.txt (or any compatible file).

Input format (MPT upstream):
    Name: <voice-name>
    Gender: <Male|Female>

Blank lines and lines starting with # are ignored.

Usage:
    python scripts/update_edge_voices.py
    python scripts/update_edge_voices.py path/to/voice-list.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def parse_voice_list(text: str) -> list[dict[str, str]]:
    voices: list[dict[str, str]] = []
    seen: set[str] = set()
    lines = text.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            i += 1
            continue

        # Expect "Name: ..." followed by "Gender: ..."
        if line.lower().startswith("name:"):
            name = line.split(":", 1)[1].strip()
            gender = ""
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line.lower().startswith("gender:"):
                    gender = next_line.split(":", 1)[1].strip().capitalize()
                    i += 1
            if name and gender and name not in seen:
                seen.add(name)
                voices.append({"name": name, "gender": gender})
        i += 1

    return voices


def default_voice_list_path() -> Path:
    here = Path(__file__).resolve().parent.parent
    mpt_docs = here.parent / "MoneyPrinterTurbo" / "docs" / "voice-list.txt"
    if mpt_docs.exists():
        return mpt_docs
    return here / "docs" / "voice-list.txt"


def main() -> int:
    if len(sys.argv) > 2:
        print("Usage: update_edge_voices.py [path/to/voice-list.txt]", file=sys.stderr)
        return 1

    src = Path(sys.argv[1]) if len(sys.argv) == 2 else default_voice_list_path()
    if not src.exists():
        print(f"Error: voice list not found: {src}", file=sys.stderr)
        return 1

    text = src.read_text(encoding="utf-8")
    voices = parse_voice_list(text)
    if not voices:
        print(f"Error: no voices parsed from {src}", file=sys.stderr)
        return 1

    dest = Path(__file__).resolve().parent.parent / "src/mpt_autopilot/batch/data/edge_voices.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(voices, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(voices)} voices to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
