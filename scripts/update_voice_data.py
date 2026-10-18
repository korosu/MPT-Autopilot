#!/usr/bin/env python3
"""
Regenerate the bundled voice reference data and/or verify a config.yaml's
voice pools against it.

Sources (both from a local MoneyPrinterTurbo checkout, sibling of this repo
by default):
- Edge voices:  docs/voice-list.txt   → src/mpt_autopilot/data/edge_voices.json
                (format: "Name: <name>" / "Gender: <Male|Female>" pairs)
- Gemini voices: the GEMINI_TTS_VOICES constant in app/services/voice.py
                → src/mpt_autopilot/data/gemini_voices.json
                (the constant is a tuple of ("Name", "Style") pairs; parsing
                is deliberately a dumb regex so it keeps working regardless
                of quote style / trailing commas in the source)

Usage:
    python scripts/update_voice_data.py                        # regenerate both
    python scripts/update_voice_data.py path/to/voice-list.txt # edge only, custom path
    python scripts/update_voice_data.py --verify-config ../mpt-autopilot/config.yaml

Exit codes: 0 ok, 1 usage/data error or verification failure.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Both targets live in the shared data package so pilot (assignment pre-flight)
# and batch (edge alias pool) read the same files the verifier regenerates.
DATA_DIR = REPO / "src" / "mpt_autopilot" / "data"
EDGE_DEST = DATA_DIR / "edge_voices.json"
GEMINI_DEST = DATA_DIR / "gemini_voices.json"

# GEMINI_TTS_VOICES = (\n    ("Zephyr", "Bright"),\n    ...
_GEMINI_PAIR_RE = re.compile(r'\(\s*"?([A-Za-z]+)"?\s*,\s*"?[^")]*"?\s*\)')


def parse_voice_list(text: str) -> list[dict[str, str]]:
    """Parse MPT's docs/voice-list.txt ("Name:"/"Gender:" line pairs)."""
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


def parse_gemini_voices(text: str) -> list[str]:
    """Extract the voice names from the GEMINI_TTS_VOICES tuple literal."""
    match = re.search(r"GEMINI_TTS_VOICES\s*=\s*\((.*?)\)", text, re.DOTALL)
    if not match:
        return []
    return _GEMINI_PAIR_RE.findall(match.group(1))


def default_mpt_checkout() -> Path:
    """Sibling MoneyPrinterTurbo checkout (same parent dir as this repo)."""
    return REPO.parent / "MoneyPrinterTurbo"


def regenerate(edge_list: Path | None, mpt_checkout: Path) -> int:
    wrote_any = False

    if edge_list is not None:
        if not edge_list.exists():
            print(f"Error: voice list not found: {edge_list}", file=sys.stderr)
            return 1
        voices = parse_voice_list(edge_list.read_text(encoding="utf-8"))
        if not voices:
            print(f"Error: no voices parsed from {edge_list}", file=sys.stderr)
            return 1
        EDGE_DEST.write_text(
            json.dumps(voices, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Wrote {len(voices)} Edge voices to {EDGE_DEST}")
        wrote_any = True

    voice_py = mpt_checkout / "app" / "services" / "voice.py"
    if voice_py.exists():
        gemini = parse_gemini_voices(voice_py.read_text(encoding="utf-8"))
        if gemini:
            GEMINI_DEST.write_text(
                json.dumps(gemini, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"Wrote {len(gemini)} Gemini voices to {GEMINI_DEST}")
            wrote_any = True
        else:
            print(f"Warning: no GEMINI_TTS_VOICES pairs parsed from {voice_py}", file=sys.stderr)
    else:
        print(f"Note: {voice_py} not found — gemini list left unchanged", file=sys.stderr)

    return 0 if wrote_any else 1


def verify_config(config_path: Path) -> int:
    import yaml

    from mpt_autopilot.data import voice_data

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    langs = cfg.get("langs") or {}
    if not isinstance(langs, dict) or not langs:
        print(f"Error: no 'langs:' block in {config_path}", file=sys.stderr)
        return 1

    failed = False
    for code, block in sorted(langs.items()):
        raw = (block or {}).get("voices")
        if raw is None:
            print(f"langs.{code}.voices: MISSING — pilot's pre-flight will refuse to run")
            failed = True
            continue
        voices = [str(v) for v in raw] if isinstance(raw, list) else [str(raw)]

        errors = voice_data.validate_pool(str(code), voices)
        for line in errors:
            print(line)
            failed = True
        if not errors:
            statuses = ", ".join(f"{v} ({voice_data.classify(v)})" for v in voices)
            print(f"langs.{code}.voices: ok — {statuses}")

    return 1 if failed else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if len(args) > 1:
        print(
            "Usage: update_voice_data.py [path/to/voice-list.txt] [--verify-config <config.yaml>]",
            file=sys.stderr,
        )  # noqa: E501
        return 1

    if "--verify-config" in flags:
        # --verify-config consumes the positional as its config path.
        try:
            idx = sys.argv.index("--verify-config")
            config_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        except ValueError:  # pragma: no cover - flag checked above
            config_arg = None
        if config_arg is None or config_arg.startswith("--"):
            print(
                "Error: --verify-config needs a path: --verify-config <config.yaml>",
                file=sys.stderr,
            )
            return 1
        return verify_config(Path(config_arg))

    edge_list = Path(args[0]) if args else default_mpt_checkout() / "docs" / "voice-list.txt"
    return regenerate(edge_list, default_mpt_checkout())


if __name__ == "__main__":
    raise SystemExit(main())
