# Changelog

All notable changes to this project are recorded in the commit history.
This file is the headline summary; per-file history reaches back into the
four repositories this one was merged from via `git log --follow`.

## 1.0.0

MPT Autopilot's first release merges four previously separate tools into a
single repository with one CLI, one config file, and one package. Every stage
also runs on its own, so you can adopt as much or as little of the chain as
you want.

### One command, one config, one package

- `mpt` replaces `refill`, `init-seen`, `batch`, `enrich`, and `upload`.
  One `uv sync`, one clone, one `--help` for the whole suite.
- `config.yaml` is now sectioned — `pilot:` / `batch:` / `enricher:` /
  `uploader:` / `pipeline:` — with a shared `telegram_prefix:` / `paths:` /
  `langs:` / block every stage reads.
- A single `mpt run` chains `refill → batch → enrich → upload` per language.
- Per-language isolation: a stage failing for Spanish no longer aborts English.
- GitHub Actions runs lint, type check, **and tests** on every push/PR.

### What was fixed during the merge

- **`mpt batch --lang` derived the seen registry from the wrong directory.**
  With `batch.seen_file: ./jobs/seen.txt`, `--lang es` silently looked for
  `./seen_es.txt` (next to config.yaml) instead of `./jobs/seen_es.txt`,
  re-rendering every video that language had already produced. Fixed and
  regression-tested.
- **`bgm_type: custom` was rejected by refill's output validation**, even though
  `mpt batch --list-bgm` suggests it and `get_bgm_file()` in MoneyPrinterTurbo
  honours it. Whitelists now mirror MPT's actual handling, and `custom` is no
  longer rewritten to `random`.
- **Every stage's `--help` now shows its own examples.** They were authored but
  never passed to the subparsers — only the standalone parsers showed them.
- **`mpt upload --dry-run` used to move files.** The crash-recovery pass ran
  unconditionally; a preview deleted nothing, but the run before it did. The
  pass is now skipped in dry-run mode.
- **`--limit` had no effect on a dry run.** Now counted against the same cap a
  real run honours, so the preview matches what would actually happen.
- **Error messages now name the right key.** `enricher.max_tags`,
  `uploader.defaults.hashtag_placement`, and the tri-state
  `enricher.reasoning_*` keys no longer point at flat names that don't exist in
  the sectioned config.
- **Stale module references across docstrings** — `engine/`, `generator/`,
  `config.py` — updated to `batch/`, `pilot/`, `settings.py`.
- **One HTTP client, not two.** `requests` has been removed; the Telegram
  notifier, the batch API client, and the pilot LLM client all use `httpx`,
  matching the enricher.

### Preserved history

All four repositories were imported with `git fast-export` → path rewrite →
`git fast-import`. `git blame` and `git log --follow` reach back into each
file's original commits. Per-tool release tags stay in the archived repos;
this one starts at 1.0.0.
