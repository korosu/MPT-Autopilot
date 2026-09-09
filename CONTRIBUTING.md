# CONTRIBUTING — MPT Autopilot

Thank you for your interest in improving MPT Autopilot. This document covers
how to set up a development environment, what standards the project follows, and
what kinds of contributions are likely to be accepted.


## Before you start

- Python 3.10+ and [uv](https://docs.astral.sh/uv/) are required.
- The [README.md](README.md) explains the pipeline and the four stages at a
  high level; `docs/` has per-stage details.
- Project coding conventions and architectural rules are documented in this
  file and in the source code itself (see comments in
  `src/mpt_autopilot/config.py`, `src/mpt_autopilot/pipeline.py`, and
  `src/mpt_autopilot/cli.py`).


## Setting up

```bash
git clone https://github.com/korosu/mpt-autopilot.git
cd mpt-autopilot
uv sync --extra dev
cp config.example.yaml config.yaml
cp .env.example .env       # fill in real API keys / config values
# cp jobs/jobs.example.yaml jobs.yaml   # optional — only needed if you use scheduled jobs
```

### Verification

Before pushing, run all four quality gates:

```bash
uv run ruff check
uv run ruff format
uv run pytest
uv run pyright
```

All four run in CI on every push to `main` and on every PR, and all four must
pass before a PR can be merged.


## What the project maintains

| Area | Maintained? | Notes |
|---|---|---|
| Python source (`src/`) | Yes | Follow import and async conventions described below |
| Tests (`tests/`) | Yes | pytest; no real network calls — mock external APIs (LLM provider, uploader) rather than hitting them |
| Docs (`docs/` and `README.md`) | Yes | Keep examples in sync with `config.example.yaml` |
| Dependencies (`pyproject.toml`) | Yes | Pin via uv; justify new deps |
| CI configuration | Yes | Not open to external changes without discussion |


## Code style and conventions

- **Imports:** `from __future__ import annotations` first, then stdlib, then
  third-party, then local. The four stage packages (`pilot`, `batch`,
  `enricher`, `uploader`) **never import each other** — shared logic stays in
  top-level modules.
- **Async:** `async def` for I/O in `batch`, `enricher`, and `uploader`.
  `pilot` wraps its LLM calls via `asyncio.run()` in `execute()`.
- **Path resolution:** never use `Path("x").resolve()` against cwd. Use
  `cfg.resolve()` / `cfg.path_value()` so paths always resolve relative to
  `config.yaml`'s directory.
- **Logging:** use `mpt_autopilot.logger`; never `print()` in stage code.
- **Error handling:** stages return clean `[ERROR]` messages — never raw
  tracebacks to the user.
- **Line length:** 100 characters (`ruff` enforces this).
- The enricher's `settings` singleton must stay **lazy** — `execute()` must be
  the first call site.
- `cli.py` must not import `httpx`, `requests`, or `yaml` at module scope;
  `_stage_hooks()` imports stage modules lazily.


## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) subjects:

```
feat: add --dry-run to the batch stage
fix: handle missing BGM file in stage run
refactor: extract retry logic into _run_with_backoff
docs: document uploader ledger rotation
ci: pin pytest version in uv.lock
```

- `fix` and `feat` should reference an issue number when one exists:
  `fix: close timeout race in uploader (#42)`
- The subject is lowercase, no trailing period.
- Scope is optional but encouraged when the change targets a single stage.


## Branching and PRs

- Create a feature branch from `main`.
- Open a PR targeting `main`.
- CI runs on every push to `main` and on every PR.
- The PR description should explain **what** changed and **why** — not a
  line-by-line summary.
- If your PR adds or changes user-facing behaviour, update `README.md` or
  `docs/` accordingly.


## What contributions are welcome

- Bug fixes and reliability improvements in any stage.
- Better error messages and clearer logging.
- Documentation corrections and expanded usage examples.
- Test coverage for un-tested or under-tested paths.
- Dependency updates with a security or compatibility rationale.

Before investing significant time in a contribution, please open an issue to
discuss the direction — this avoids duplicated or rejected work.


## What contributions are not welcome

- Adds that import one stage package from another; shared logic should live in
  top-level modules under `src/mpt_autopilot/` (see `config.py` and `notify.py`
  as examples).
- Adds that import `httpx`, `requests`, or `yaml` at module scope in `cli.py`;
  `cli.py` imports stage modules only through `_stage_hooks()`.
- Adds that instantiate the enricher's `Settings()` dataclass at import time;
  stage `execute()` must call `configure(config_path)` first.
- Commits that squash or re-write imported git history — the project preserves
  provenance from its upstream sources via `git fast-export` / `fast-import`;
  do not alter that history.


## Security

Please do **not** open a public issue for security vulnerabilities. See
[SECURITY.md](SECURITY.md) for how to report them privately.


## License

By contributing, you agree that your contributions are licensed under the
same license as the project (see [LICENSE](LICENSE)).


## Questions?

Open a GitHub Discussion or a non-security issue on
[the issue tracker](https://github.com/korosu/mpt-autopilot/issues).