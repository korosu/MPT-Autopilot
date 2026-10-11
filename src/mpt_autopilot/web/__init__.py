"""
The local web interface for `mpt` (`mpt web`).

Deliberately empty of imports. `cli.build_parser()` imports
`mpt_autopilot.web.cli` on every `mpt --help`, and this package's __init__ runs
first — so re-exporting anything from here would pull Flask into `mpt --help`
for everyone who did not install the optional `[web]` extra.
"""

from __future__ import annotations
