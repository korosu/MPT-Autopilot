"""
The wheel must carry the page's own files (W-8).

hatchling selects files by VCS tracking, so a future .gitignore line like
`static/` would silently build a wheel where `mpt web` dies on
TemplateNotFound — with every test still green, because tests run from the
source tree. This asserts the three paths the page needs exist relative to
the installed package, which is what the wheel build depends on.
"""

from __future__ import annotations

from pathlib import Path

from mpt_autopilot import web

_WEB_DIR = Path(web.__file__).parent


def test_the_wheel_files_are_present():
    for relative in ("templates/index.html", "static/app.js", "static/app.css"):
        assert (_WEB_DIR / relative).is_file(), f"{relative} is missing from the web package"
