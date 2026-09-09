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

__version__ = "1.0.6"
