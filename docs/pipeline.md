# `mpt run` — the whole pipeline in one command

`mpt run` chains the four stages in the only order that makes sense, once per
language:

```
refill  →  batch  →  enrich  →  upload
```

It exists because that is what a farm actually does on a schedule, and because
four separate cron entries had four separate ways to go wrong: a batch run that
started before refill finished, an upload that ran against a folder enrich had
not touched yet, a language that silently stopped being produced because one
crontab line had a typo.

```bash
mpt run                       # every language in config.yaml
mpt run --lang en             # one language
mpt run --lang en --lang es   # two, in that order
mpt run --all-langs           # explicit form of the default
mpt run --dry-run             # show what would happen, touch nothing
```

Everything runs **in one process**: each stage is called through its own
`execute()` function, not spawned as a subprocess. There is no startup cost per
stage, and a stage behaves exactly as it does when you invoke it directly.

## What each stage receives

`mpt run` does not expose every stage flag — it derives them, which is the point
of having one command:

| Stage | What `mpt run` passes |
|---|---|
| `refill` | the language; config defaults for count/threshold, no `--force` |
| `batch` | the language (so jobs/seen/output all get its suffix), `--dry-run` |
| `enrich` | the language's exports directory, `--dry-run` |
| `upload` | the account for the language, `--dry-run`, `--limit` |

If you need a non-default flag — `--force` on refill, `--platform tiktok` on
enrich — run that stage on its own. `mpt run` is for the routine path.

### How the language wires the stages together

The shared `langs:` block in config.yaml is what makes this work. A language's
`file_suffix` derives every name:

```yaml
langs:
  en:
    file_suffix: ""     # jobs.yaml,    seen.txt,    exports/
  es:
    file_suffix: "_es"  # jobs_es.yaml, seen_es.txt, exports_es/
```

So `mpt run --lang es` refills `jobs_es.yaml`, renders into `exports_es/`, and
enriches that same `exports_es/` — the handoff needs no configuration because
both stages compute the same path from the same suffix.

### Which account a language uploads to

By default the account name **is** the language code, so if your accounts.yaml
has `en:` and `es:` keys there is nothing to configure. When your channels are
named something else, map them:

```yaml
pipeline:
  accounts:
    en: main-channel
    es: spanish-channel
```

### The one join you have to make by hand

`batch` writes into `batch.output_dir` plus the language suffix. `upload` reads
from the account's `videos_dir` in accounts.yaml. **Those two have to be the same
directory**, and nothing checks it for you — the uploader is deliberately free to
upload from a folder no batch run produced.

So for the config above:

```yaml
# config.yaml
batch:
  output_dir: "/srv/mpt/exports"     # → exports/ and exports_es/
```

```yaml
# accounts.yaml
accounts:
  en:
    videos_dir: "/srv/mpt/exports"
  es:
    videos_dir: "/srv/mpt/exports_es"
```

`mpt run --dry-run` is how you catch a mismatch: upload reports the files it
would send, and an empty list right after batch listed jobs to render is the
symptom.

## Selecting stages

```bash
mpt run --only batch --only enrich      # just render and tag
mpt run --skip refill                   # render what's already queued
mpt run --skip upload                   # produce everything, upload later
```

`--only` and `--skip` are both repeatable and combine (`--only` first, then
`--skip` removes from that set). Order does not matter: stages always execute in
pipeline order, so `--only upload --only batch` still runs batch before upload.

## `--dry-run`

`--dry-run` is passed to every stage that has one, and prints what would happen
without rendering a video, calling an LLM, or uploading anything.

**refill is skipped entirely in a dry run.** Generating ideas has no dry
equivalent — the LLM call is the whole operation — and doing it for real during
a `--dry-run` would silently mutate your jobs queue. The run says so:

```
[run] --dry-run: skipping refill (no dry-run mode for idea generation)
```

Use `mpt run --dry-run` before the first real run of a new config, and after
changing paths. It exercises the same config loading, language resolution, and
account lookup as a real run, so a wrong path surfaces in seconds instead of
after a 40-minute render.

## Failure handling

By default a failing stage stops **that language** and moves on to the next one:

```
[run] es → batch returned 1 — stopping this language (use --continue-on-error to keep going)
```

This is deliberate. Enriching or uploading after a failed render would work on a
half-finished directory, and the later stages' output would be misleading. But a
Spanish failure is no reason to skip English, so language isolation is absolute:
each language gets its own attempt at every stage.

`--continue-on-error` runs every remaining stage anyway. Useful when you want a
full picture of what is broken rather than the first symptom.

A stage that raises instead of returning is caught and recorded as a failure —
one stage crashing never takes the rest of the run with it.

## Exit codes

Every run ends with a summary and one aggregate exit code:

```
============================================================
[run] summary
============================================================
  en     refill     ok
  en     batch      ok
  en     enrich     ok
  en     upload     ok
  es     refill     ok
  es     batch      failed
```

| Code | Meaning |
|---|---|
| 0 | everything succeeded, or was legitimately skipped |
| 1 | at least one stage failed |
| 2 | nothing failed, but at least one upload stopped on YouTube's daily quota |

Failure dominates a quota stop: if one language failed and another hit quota,
the exit code is 1. A quota stop is not an error — it is YouTube's normal daily
ceiling, and it gets its own code so a cron wrapper can tell "try again
tomorrow" apart from "something is broken".

`enrich` skipping a language because its exports directory does not exist yet is
reported as `ok (skipped (no exports dir))`, not a failure. Nothing has been
rendered for that language yet, which is a normal state on a first run.

## Scheduling it

One crontab line replaces four:

```cron
# every 6 hours, every configured language
0 */6 * * * cd /srv/mpt && /usr/local/bin/mpt --config /srv/mpt/config.yaml run >> /srv/mpt/logs/run.log 2>&1
```

`--config` with an absolute path is worth using here even when the working
directory looks right: every relative path in config.yaml resolves against the
config file's own location, so the run behaves identically no matter where cron
starts it. The `.env` beside that config.yaml is picked up automatically.

To treat a quota stop as success and only alert on real failures:

```bash
mpt --config /srv/mpt/config.yaml run; code=$?; [ $code -eq 2 ] && exit 0; exit $code
```

## Related

- [pilot.md](pilot.md) — `mpt refill`, `mpt init-seen`
- [batch.md](batch.md) — `mpt batch`
- [enricher.md](enricher.md) — `mpt enrich`
- [uploader.md](uploader.md) — `mpt upload`
- [migration.md](migration.md) — coming from the four separate tools
