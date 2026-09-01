"""
One-off import rewriter (Etap 3): rewrite the merged sources' import paths from
the four original package namespaces to `mpt_autopilot.*`.

Also rewrites the dotted paths used in monkeypatch/patch target strings, which
are just as load-bearing as the import statements themselves.

Longest keys are applied first so `mpt_batch.engine.notify` is not clobbered by
a shorter `mpt_batch.engine` prefix.

Usage:  python rewrite_imports.py <repo-root>
"""

from __future__ import annotations

import sys
from pathlib import Path

# Module-level dotted paths, mapped one-to-one.
MODULES: dict[str, str] = {
    # ── batch ────────────────────────────────────────────────────────────────
    "mpt_batch.engine.settings": "mpt_autopilot.batch.settings",
    "mpt_batch.engine.api": "mpt_autopilot.batch.api",
    "mpt_batch.engine.bgm": "mpt_autopilot.batch.bgm",
    "mpt_batch.engine.state": "mpt_autopilot.batch.state",
    "mpt_batch.engine.voices": "mpt_autopilot.batch.voices",
    "mpt_batch.engine.notify": "mpt_autopilot.notify",
    "mpt_batch.engine.seen": "mpt_autopilot.seen",
    "mpt_batch.batch": "mpt_autopilot.batch.run",
    # ── pilot ────────────────────────────────────────────────────────────────
    "shorts_pilot.generator.settings": "mpt_autopilot.pilot.settings",
    "shorts_pilot.generator.llm": "mpt_autopilot.pilot.llm",
    "shorts_pilot.generator.prompt": "mpt_autopilot.pilot.prompt",
    "shorts_pilot.generator.jobs": "mpt_autopilot.pilot.jobs",
    "shorts_pilot.generator.seen": "mpt_autopilot.seen",
    "shorts_pilot.generator.lock": "mpt_autopilot.lock",
    "shorts_pilot.generator.notify": "mpt_autopilot.notify",
    "shorts_pilot.refill": "mpt_autopilot.pilot.refill",
    "shorts_pilot.init_seen": "mpt_autopilot.pilot.init_seen",
    # ── enricher ─────────────────────────────────────────────────────────────
    "hashtag_enricher.enricher.config": "mpt_autopilot.enricher.settings",
    "hashtag_enricher.enricher.postprocess": "mpt_autopilot.enricher.postprocess",
    "hashtag_enricher.enricher.reader": "mpt_autopilot.enricher.reader",
    "hashtag_enricher.enricher.writer": "mpt_autopilot.enricher.writer",
    "hashtag_enricher.enricher.llm": "mpt_autopilot.enricher.llm",
    "hashtag_enricher.enricher.logger": "mpt_autopilot.logger",
    "hashtag_enricher.enricher.notify": "mpt_autopilot.notify",
    "hashtag_enricher.enrich": "mpt_autopilot.enricher.run",
    # the broken pre-merge patch target in the enricher notify tests
    "hashtag_enricher.notify": "mpt_autopilot.notify",
    # ── uploader ─────────────────────────────────────────────────────────────
    "yt_uploader.engine.settings": "mpt_autopilot.uploader.settings",
    "yt_uploader.engine.uploader": "mpt_autopilot.uploader.uploader",
    "yt_uploader.engine.metadata": "mpt_autopilot.uploader.metadata",
    "yt_uploader.engine.ledger": "mpt_autopilot.uploader.ledger",
    "yt_uploader.engine.notify": "mpt_autopilot.notify",
    "yt_uploader.upload": "mpt_autopilot.uploader.run",
}

# `from <package> import a, b` forms, where the names come from several
# different modules after the merge and a single prefix swap is not enough.
PACKAGE_FORMS: dict[str, str] = {
    "from mpt_batch.engine import bgm, notify, seen, state, voices": (
        "from mpt_autopilot import notify, seen\nfrom mpt_autopilot.batch import bgm, state, voices"
    ),
    "from mpt_batch.engine import voices": "from mpt_autopilot.batch import voices",
    "from mpt_batch.engine import seen": "from mpt_autopilot import seen",
    "from mpt_batch.engine import state": "from mpt_autopilot.batch import state",
    "from shorts_pilot.generator import jobs, seen": (
        "from mpt_autopilot import seen\nfrom mpt_autopilot.pilot import jobs"
    ),
    "from shorts_pilot.generator import seen": "from mpt_autopilot import seen",
    "from shorts_pilot.generator import llm": "from mpt_autopilot.pilot import llm",
    "from yt_uploader.engine import notify": "from mpt_autopilot import notify",
}


def rewrite(text: str) -> str:
    for old, new in PACKAGE_FORMS.items():
        text = text.replace(old, new)
    for old in sorted(MODULES, key=len, reverse=True):
        text = text.replace(old, MODULES[old])
    return text


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    changed = 0
    for path in sorted((root / "src").rglob("*.py")) + sorted((root / "tests").rglob("*.py")):
        original = path.read_text(encoding="utf-8")
        updated = rewrite(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed += 1
            print(f"rewrote {path.relative_to(root)}")
    print(f"{changed} file(s) rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
