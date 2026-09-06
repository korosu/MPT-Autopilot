# AGENTS.md

MPT Autopilot standing orders. Read [CLAUDE.md](CLAUDE.md) for project
overview, architecture, and the stage contract — this file is the imperative
half. CLAUDE.md and AGENTS.md must always be identical: any change to one is a
change to both.

## Honesty rule

Never claim a file was created, modified, or deleted unless a verification step
(`read`, `glob`, `Test-Path`, etc.) confirmed it. If a tool returned an error,
the file still exists, or the result is uncertain — say so explicitly and
investigate. Do not report success before checking.

## Behavioral guidelines

These rules bias toward caution. Use judgment on trivial tasks.

### 1. Think Before Coding

Don't assume. Don't hide confusion. Surface tradeoffs.

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.

### 3. Surgical Changes

Touch only what you must. Clean up only your own mess.

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

Remove imports/variables/functions that YOUR changes made unused. Don't remove
pre-existing dead code unless asked.

### 4. Goal-Driven Execution

Define success criteria. Loop until verified.

- "Fix a bug" → write a failing test, then make it pass.
- "Refactor X" → ensure tests pass before and after.
- State a brief plan before multi-step work.

---

## Version bump policy

Before finishing any task that changes tracked project files, update the version
in `pyproject.toml` exactly once:

- Bugfix, refactor, docs, config → bump PATCH by `0.0.1`.
- New user-visible content or new logic → bump MINOR by `0.1.0`, reset PATCH to
  `0`.

## Git / PR conventions

- Use [Conventional Commits](https://www.conventionalcommits.org/) subjects
  (`feat:`, `fix:`, `refactor:`, `docs:`, etc.).
- CI runs on pushes to `main`, PRs targeting `main`, and tags matching `v*`.
- The test suite is the only automated quality gate. Run it before pushing.

## Quality gates (mandatory on every code submission)

After any change to tracked project files, run **all three** commands and
confirm each exits 0 before submitting:

```bash
uv run ruff check
uv run ruff format
uv run pyright
```

All three must pass. Fix every issue before considering the work done.

## Conventions

- **Python style:** `snake_case` for functions/variables, `PascalCase` for
  classes, `UPPERCASE` for module constants.
- **Imports:** are `from __future__ import annotations` first, stdlib third,
  third-party last, local last. The four stage packages never import each other.
- **Async:** use `async def` for I/O in batch, enricher, and uploader stages;
  pilot's LLM calls are sync-wrapped via `asyncio.run()` in `execute()`.
- **Error handling:** stages return clean `[ERROR]` messages — never raw
  tracebacks to the user.
- **Path handling:** never use `Path(x).resolve()` against cwd. Use
  `cfg.resolve()` / `cfg.path_value()` so paths always resolve relative to
  `config.yaml`'s directory.
- **Logging:** use the shared `mpt_autopilot.logger`; never `print()` in stage
  code.
- **Comments and docs:** explain why, not what. Link to the rationale or the
  rule this code enforces — don't restate the code.

## Gotchas

- The enricher's `settings` singleton is imported at module scope. Instantiating
  it eagerly (`Settings()`) makes `mpt --help` require a `config.yaml`. Always
  lazy — let `execute()` call `configure(config_path)` first.
- `cli._stage_hooks(name)` imports the stage module inside the function. This is
  the only sanctioned exception to top-level imports. Don't add new top-level
  imports of httpx/requests/yaml in `cli.py`.
- `reasoning_enabled` is tri-state: `None` (absent, send nothing), `True` (send
  `"low"`), `False` (send `"none"`). `None` and `False` are different — don't
  conflate them with `get("reasoning_enabled", False)`.
- SQLite WAL mode creates `-wal` and `-shm` sidecars during uploader runs. The
  ledger path in `.gitignore` covers them, but moving the ledger file without
  also migrating WAL residuals resets dedup state.
- `seen.py` uses a file lock. Two `pytest` processes pointing at the same
  `seen.txt` will deadlock. Use unique temp paths in tests.

## Preserved history

The four source repositories were imported with `git fast-export` → stream
rewrite → `git fast-import`. Do not re-import, rewrite, or squash that
history — `git log`, `git blame`, and `git log --follow` already traverse into
original commits.

## Project overview

MPT Autopilot is an end-to-end automation suite for [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo): one `mpt` command drives the full pipeline from a content idea to an uploaded YouTube Short.

**Pipeline (in order):**

1. **`pilot` (`mpt refill`)** — Calls an LLM to generate video idea objects and appends them to the jobs queue. Also: `mpt init-seen` registers existing `.mp4` files so refill won't repeat them.
2. **`batch` (`mpt batch`)** — Reads `jobs.yaml` (or `jobs_<lang>.yaml`), submits each enabled job to the MoneyPrinterTurbo API, polls until completion, and copies finished videos + script.json to the exports directory. Handles crash-recovery via an in-progress registry and consecutive-failure aborts.
3. **`enricher` (`mpt enrich`)** — Reads finished videos (or sidecar `.json` files), calls an LLM to generate platform-appropriate hashtags, and writes them back as a `hashtags` block in the sidecar.
4. **`uploader` (`mpt upload`)** — Reads the sidecar metadata and uploads the video to YouTube via `youtubeuploader`, with a SQLite dedup ledger.

All four stages are wired together by **`pipeline.py`** (`mpt run`), which iterates per language and per stage, respecting `--only`, `--skip`, `--continue-on-error`, and `--dry-run`.

**Shared building blocks** (top-level under `src/mpt_autopilot/`):

- **`config.py`** — single `config.yaml` loader shared by every stage; handles path resolution relative to the config file, section access, and `.env` loading via `dotenv`.
- **`notify.py`** — Telegram alert helper; accepts any settings object with `telegram_token` / `telegram_chat_id` / `telegram_prefix`.
- **`seen.py`** — dedup registry (text file, one filename per line); supports rotation when the file exceeds a configurable size threshold.
- **`lock.py`** — cross-platform advisory file lock via atomic exclusive file creation.
- **`logger.py`** — file + stdout/stderr logger with size-based rotation (`RotatingFileHandler`).

**Key config path (`config.yaml`):**

```
telegram_prefix      # shared default; stages can override
langs:               # shared language block (code → {label, file_suffix, ...})
paths:               # shared path block (jobs_dir, seen_dir, exports_dir, videos_dir)
pilot:               # pilot stage settings (LLM, generation, seen_max_mb)
batch:               # batch stage settings (API URL, mpt_storage, seen_max_mb, ...)
enricher:            # enricher stage settings (platform, tags, prompts)
uploader:            # uploader stage settings (accounts, auth, ledger path)
pipeline:            # pipeline orchestration (accounts map, ...)
```

Each stage also has its own `Settings` dataclass that reads its section from the
shared loader and validates required fields.

## Stage contract

- Stages return clean exit codes (0 = ok, 1 = failure, 2 = quota stop) — never
  raw tracebacks to the user.
- `cli.py` must not import httpx/requests/yaml at module scope; only
  `_stage_hooks()` imports stage modules lazily.
- The enricher's `settings` singleton must stay lazy: `execute()` calls
  `configure(config_path)` before anything touches it, otherwise `mpt --help`
  requires a `config.yaml`.
- `seen.py` rotation threshold is configurable per stage via
  `pilot.seen_max_mb` / `batch.seen_max_mb` (default: 64 MB).
