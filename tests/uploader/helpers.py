"""
Shared helpers for the uploader tests.

`youtubeuploader` is looked up with a platform check: on Windows only
.exe/.cmd/.bat count as executable, on POSIX the +x bit does. A fixture binary
therefore has to be named for the platform the tests happen to run on —
hardcoding an extensionless name made every account-readiness test fail on
Windows.
"""

from __future__ import annotations

import os
from pathlib import Path


def make_uploader_binary(tmp_path: Path, name: str = "youtubeuploader") -> Path:
    """
    Create a stub uploader binary that `find_uploader_binary` accepts on this
    platform, and return its path.
    """
    binary = tmp_path / (f"{name}.cmd" if os.name == "nt" else name)
    binary.write_text("@echo off\n" if os.name == "nt" else "#!/bin/bash\n")
    binary.chmod(0o755)
    return binary
