# CLAUDE.md

> Shared rules (environment, style, workflow, CI, summaries, plugins, agents): see `../CLAUDE.md`.

## Project Overview

MPT Autopilot automates MoneyPrinterTurbo end to end: generate video ideas, render
them, tag them, upload them. It is a merge of four repositories that used to be
installed and configured separately:

| Was | Now |
|---|---|
| shorts-pilot | `pilot/` — `mpt refill`, `mpt init-seen` |
| mpt-batch | `batch/` — `mpt batch` |
| hashtag-enricher | `enricher/` — `mpt enrich` |
| yt-shorts-uploader | `uploader/` — `mpt upload` |

`mpt run` chains all four per language. See `docs/migration.md` for the mapping in
user-facing terms.

## Architecture

```
src/mpt_autopilot/
  cli.py           # the only entry point; registers every stage
  pipeline.py      # `mpt run` — chains the stages per language
  config.py        # shared sectioned config.yaml + .env loader
  seen.py          # shared dedup registry (all stages)
  notify.py        # shared Telegram alerts (all stages)
  lock.py          # file lock with timeout
  logger.py        # stdout + rotating file logging
  pilot/     {refill, init_seen, jobs, llm, prompt, settings}.py
  batch/     {run, api, bgm, state, voices, settings}.py + data/edge_voices.json
  enricher/  {run, settings, llm, postprocess, reader, writer}.py
  uploader/  {run, settings, uploader, metadata, ledger}.py
```

The four stage packages do not import each other. Everything they share goes
through the seven top-level modules — that separation is what keeps a change in
one stage from silently altering another.

## The stage contract

Every stage entry point (`pilot/refill.py`, `pilot/init_seen.py`, `batch/run.py`,
`enricher/run.py`, `uploader/run.py`, `pipeline.py`) exposes exactly four things:

```python
def add_arguments(parser) -> parser   # stage flags ONLY — never --config
EPILOG = "..."                        # examples, shown in --help
def build_parser() -> parser          # standalone parser; adds --config
def execute(args, config_path=None) -> int   # returns an exit code
```

Adding a flag means editing `add_arguments` and nothing else: the `mpt`
subparser and the standalone parser both build from it, so they cannot drift.

`execute` **returns** its exit code and never calls `sys.exit` — that is what lets
`pipeline.py` call stages in-process and aggregate their results. Only `main()`
exits, via `sys.exit(execute(build_parser().parse_args()))`.

Two subtleties that look like mistakes and are not:

- Each subparser's `--config` uses `dest="stage_config"`, not `dest="config"`. A
  shared dest would let the subparser's own default overwrite the root value, so
  `mpt --config X batch` would silently read `./config.yaml`. Dispatch reads
  `getattr(args, "stage_config", None) or args.config`.
- `cli._stage_hooks(name)` imports a stage's module **inside the function**. This
  is the one sanctioned exception to top-level imports: `mpt --help` must work in
  a directory with no config.yaml and without importing httpx/requests/yaml for
  five stages you are not running. `tests/common/test_cli.py` locks this in by
  running every `--help` from an empty directory.

## Configuration

One `config.yaml`, sectioned. `config.example.yaml` is the authoritative
reference — read it before changing any loader.

```yaml
telegram_prefix: "mpt-autopilot"   # shared; any section may override
paths:    {jobs_dir, seen_dir, exports_dir}
langs:    {en: {label, file_suffix, voices, job_defaults, theme_list}, ...}
pilot:    {generation: {count, threshold, ...}, scan_dirs}
batch:    {api_url, mpt_storage, mpt_songs_dir, output_dir, seen_file, voices, ...}
enricher: {platform, min_tags, max_tags, banned_tags, always_include, prompt_*}
uploader: {uploader_binary, meta_dir, defaults: {...}}
pipeline: {accounts: {<lang>: <account>}}
```

Invariants worth knowing before you touch a settings module:

- **Relative paths resolve against config.yaml's directory**, never cwd. This is
  what makes `mpt --config /srv/mpt/config.yaml run` behave identically from any
  working directory, and it is easy to break by reaching for `Path(x).resolve()`.
  Use `cfg.resolve()` / `cfg.path_value()`. Covered by
  `test_paths_resolve_against_config_not_cwd`.
- **`langs:` is top-level, deliberately.** pilot reads the whole entry, batch reads
  only `file_suffix`, and `mpt run` derives jobs/seen/exports names from that same
  suffix. One source of truth is the point; do not move it under a section.
- **`reasoning_enabled` is tri-state** in both pilot and enricher settings:
  `None` / `True` / `False`. Absent means "send nothing", which is not the same as
  `false` ("explicitly turn thinking off"). Read it with `get("reasoning_enabled")`
  and no default.
- **The enricher's `settings` singleton must stay lazy.** The stage imports it at
  module scope, so instantiating eagerly would make `mpt --help` require a
  config.yaml. `execute()` calls `configure(config_path)` first; the module-level
  `settings` is a `cast`-wrapped proxy that reads nothing until first attribute
  access.

## Exit codes

Load-bearing, and the uploader's original contract:

- `0` — succeeded, or nothing to do
- `1` — failure
- `2` — stopped on YouTube's daily upload quota

`pipeline.py` aggregates with failure taking precedence over a quota stop
(`1 > 2 > 0`), because a quota stop is a normal daily ceiling and a cron wrapper
needs to tell "retry tomorrow" apart from "something is broken".

## Testing

- Test packages mirror the source: `tests/{common,pilot,batch,enricher,uploader}/`.
  Every one needs an `__init__.py` — `test_settings.py` and `test_notify.py` exist
  in more than one directory, and without the packages pytest's rootdir-relative
  module names collide.
- `tests/uploader/helpers.py` builds the fake `youtubeuploader` binary. On Windows
  `_is_executable` requires a `.exe`/`.cmd`/`.bat` extension, so an extensionless
  stub fails every account-readiness check. Always go through
  `make_uploader_binary(tmp_path)`.
- `tests/common/test_config.py` and `test_cli.py` are the regression net for the
  two things the merge made fragile: section isolation in the loader, and every
  `--help` working with no config.yaml present.

## Known tech debt

Both `httpx` and `requests` ship. The enricher's LLM client is built on
`httpx.Client` with its own backoff around `httpx.HTTPStatusError`; the shared
`notify` uses `requests`. Rewriting either was out of scope for the merge.
Collapsing onto one client is a worthwhile follow-up — do it as its own change,
with the enricher's retry tests as the guard.

## Preserved history

The four repositories were imported with `git fast-export` → stream rewrite →
`git fast-import`, so `git log`, `git blame`, and `git log --follow` on any file
reach back into its original repository's commits with the original authors and
dates. Imported commits already touch the final monorepo paths — there is no
`vendor/` prefix and no rename commit to follow through.

Do not re-import, rewrite, or squash that history. `tools/import_history.py` and
`tools/rewrite_imports.py` are the one-off tools that did it, kept for the record.

Tags were not imported: three of the four repositories independently tagged
`v1.0.0`. Per-tool release history stays in the archived repositories; this one
starts at `v1.0.0`.
