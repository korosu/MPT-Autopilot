"""Tests for the mpt web interface.

Flask is an optional extra (`uv sync --extra web`). In an environment without
it the whole package skips — loudly, with the reason in pytest's skip line —
instead of erroring on import. CI installs the extra, so these always run
there.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "flask", reason="mpt web tests need the optional [web] extra: uv sync --extra web"
)
