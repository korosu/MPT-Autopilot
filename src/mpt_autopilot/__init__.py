"""
MPT Autopilot — an end-to-end automation suite for MoneyPrinterTurbo.

Stages, in pipeline order:
    pilot     — generate video ideas and refill the jobs queue (LLM)
    batch     — render pending jobs through the MoneyPrinterTurbo API
    enricher  — generate hashtags for rendered videos (LLM)
    uploader  — upload rendered videos to YouTube

Shared building blocks live at the top level: config, notify, seen, lock,
logger.
"""

from __future__ import annotations

# Read by cli.py's --version. Must match pyproject.toml's version (AGENTS.md
# version bump policy: pyproject is the source of truth, this is the display
# copy — bump both together). It drifted from 1.8.2 through 1.16.0 before the
# 1.17.0 bump caught it.
__version__ = "1.17.0"
