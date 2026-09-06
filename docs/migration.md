# Migrating from the four separate tools

MPT Autopilot is a merge of four repositories that used to be installed and
configured independently:

| Was | Now |
|---|---|
| [shorts-pilot](https://github.com/korosu/shorts-pilot) | the **pilot** stage — `mpt refill`, `mpt init-seen` |
| [mpt-batch](https://github.com/korosu/mpt-batch) | the **batch** stage — `mpt batch` |
| [hashtag-enricher](https://github.com/korosu/hashtag-enricher) | the **enricher** stage — `mpt enrich` |
| [yt-shorts-uploader](https://github.com/korosu/yt-shorts-uploader) | the **uploader** stage — `mpt upload` |

One clone, one `uv sync`, one config file. Plus a new `mpt run` that chains all
four ([pipeline.md](pipeline.md)).

Nothing about how a stage *works* changed. The video you get out of `mpt batch`
is the video you got out of `batch`. What changed is how you invoke it and where
its settings live.

---

## 0. Breaking changes — read this first

Four changes affect existing setups. Everything else is additive or internal.

### `telegram_prefix` default changed

The shared default is now `"mpt-autopilot"` (was `"mpt-batch"` per tool). If you
run Telegram bots that filter on `"[mpt-batch]"` or `"[hashtag-enricher]"`, your
alerts will no longer match after migration. Two options:

- **Keep the old prefixes** — set `telegram_prefix` inside each stage's section
  (e.g. `batch.telegram_prefix: "mpt-batch"`). The stage-level value wins over
  the shared default.
- **Adopt the new prefix** — delete any stage-level overrides and let the shared
  `"mpt-autopilot"` apply everywhere.

### `enricher.directory` → `enricher.videos_dir`

The hashtag-enricher config key `directory` was renamed to `videos_dir`. Old
configs with `enricher.directory:` will silently fall back to the current
directory. Rename the key in your `enricher:` section.

### `langs.<code>.file_suffix` is now required

If your old `langs:` entries omitted `file_suffix`, the new code derives it
automatically as `_` + code (e.g. `es` → `_es`). If your old entries used a
different convention (e.g. `es` instead of `_es`), add the key explicitly.
An entry with no `file_suffix` at all will get `_<code>` as its default — which
matches the previous behaviour for most users.

### `yt-uploader-ledger.sqlite` moved

The dedup ledger now lives next to config.yaml instead of in the working
directory. **Move your existing ledger there** — if you leave it behind, the
first run starts from an empty ledger and will try to re-upload videos it has
already published. The file is the same schema; just rename and move it. See
[section 4](#4-accountsyaml-and-the-upload-ledger) below.

---

## 1. Commands

Every old entry point is gone. There is one command, `mpt`, with subcommands:

| Old | New |
|---|---|
| `refill --lang en` | `mpt refill --lang en` |
| `init-seen --dir /videos` | `mpt init-seen --dir /videos` |
| `batch --jobs jobs.yaml` | `mpt batch --jobs jobs.yaml` |
| `enrich --dir ./exports` | `mpt enrich --dir ./exports` |
| `upload --account en` | `mpt upload --account en` |
| — | `mpt run` (new) |

Stage flags are unchanged. `--config` now works both before and after the
subcommand, so `mpt --config /srv/mpt/config.yaml batch` and
`mpt batch --config /srv/mpt/config.yaml` are equivalent; the one after the
subcommand wins if you pass both.

**Update your crontab.** Old lines calling `refill`, `batch`, `enrich`, or
`upload` will fail with "command not found" after you uninstall the old tools.
Four lines can usually become one:

```cron
0 */6 * * * cd /srv/mpt && /usr/local/bin/mpt --config /srv/mpt/config.yaml run >> /srv/mpt/logs/run.log 2>&1
```

---

## 2. Four config.yaml files become one, in sections

Take your four old config.yaml files and move each one's keys under its stage's
section. The key names themselves did not change.

```yaml
# OLD — mpt-batch/config.yaml            # NEW — config.yaml
api_url: "http://127.0.0.1:8080"         batch:
mpt_storage: "/root/MPT/storage"           api_url: "http://127.0.0.1:8080"
output_dir: "./exports"                    mpt_storage: "/root/MPT/storage"
                                           output_dir: "./exports"
```

The full sectioned structure, with every key documented, is in
[`config.example.yaml`](../config.example.yaml). Start from that file rather than
merging by hand:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
cp accounts.example.yaml accounts.yaml
```

then copy your real values across. A stage only reads its own section, so a
`batch:` key can never collide with an `enricher:` key of the same name — which
is exactly what made merging four flat files awkward before.

### `langs:` is now shared, at the top level

**This is the one change that needs a decision from you.** `langs:` is *not*
under `pilot:` or `batch:` — it sits at the top level and every stage reads the
same block:

```yaml
langs:
  en:
    label: English
    file_suffix: ""
    # ... voices, job_defaults, theme_list — pilot reads these
  es:
    label: Spanish
    file_suffix: "_es"
```

pilot reads the whole entry; batch reads only `file_suffix`; `mpt run` needs one
answer for both. Previously shorts-pilot and mpt-batch each had their own copy.

> **If those two copies disagreed, they now collapse into one.** In particular,
> if shorts-pilot said `file_suffix: "_es"` while mpt-batch said
> `file_suffix: "es"`, pick one — and make sure the existing files on disk match
> it, because `file_suffix` is what derives `jobs_es.yaml`, `seen_es.txt`, and
> `exports_es/`. Compare your two old configs before you delete them.

### Shared `paths:` and `telegram_prefix:`

Two more things moved up to the top level, both optional:

```yaml
telegram_prefix: "mpt-autopilot"   # any section may still override it
paths:
  jobs_dir: "./jobs"
  seen_dir: "./jobs"
  exports_dir: "./exports"
```

A stage-level key of the same name always wins over `paths:`. If you liked having
`[mpt-batch]` and `[hashtag-enricher]` as distinct Telegram prefixes, keep
setting `telegram_prefix` inside each section. Note that the shared default is
now `"mpt-autopilot"` (was `"mpt-batch"`) — see the [breaking changes](#0-breaking-changes--read-this-first)
above.

### Relative paths

Unchanged, and worth restating because it is the thing people get wrong: every
relative path in config.yaml resolves against **config.yaml's own directory**,
never your current working directory. That is what lets cron run
`mpt --config /srv/mpt/config.yaml run` from anywhere.

---

## 3. One `.env`

The four tools had four `.env` files with overlapping keys. Now there is one, and
because pilot and enricher share a provider, there is one set of LLM credentials:

```
LLM_API_KEY=...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT=120
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
```

If you deliberately ran refill and enrich on *different* providers, note that
they now share these three variables. `mpt` looks for `.env` next to config.yaml
first, then in the current directory.

---

## 4. accounts.yaml and the upload ledger

`accounts.yaml` is still a separate file — it points at OAuth secrets, so it is
the one you are most likely to keep outside the repository. Two changes:

- It is looked for **next to config.yaml** by default. Override with
  `uploader.accounts_file`.
- The SQLite dedup ledger is now `yt-uploader-ledger.sqlite`, created next to
  config.yaml rather than in the working directory. **Move your existing
  ledger there** — if you leave it behind, the first run starts from an empty
  ledger and will try to re-upload videos it has already published. The file is
  the same schema; just rename and move it.

If your accounts are already named after your language codes (`en:`, `es:`),
`mpt run` needs no further configuration. Otherwise map them:

```yaml
pipeline:
  accounts:
    en: main-channel
    es: spanish-channel
```

One thing worth double-checking while you are in these two files: each account's
`videos_dir` must be the directory `batch` renders that language into
(`batch.output_dir` plus the language's `file_suffix`). That was true before the
merge too, but you configured it in two unrelated repositories, so it is easy to
have drifted. See [pipeline.md](pipeline.md) for the exact pairing.

---

## 5. seen.txt and jobs.yaml

Both formats are unchanged. `seen.txt` is still one filename per line,
append-only; `jobs.yaml` still has the same `defaults:` / `jobs:` shape. Copy
your existing files across as they are — there is no conversion step, and no
re-running of `mpt init-seen` needed.

Nothing in the merge rewrites them, so you can point the new config at your
existing jobs directory and keep going mid-queue.

### ⚠️ seen-file location: check your `batch.seen_file`

The merge fixed a bug where `--lang` derived the per-language seen file
(e.g. `seen_es.txt`) from **config.yaml's directory** instead of the directory
of the configured `seen_file`. If your old config had:

```yaml
batch:
  seen_file: "./jobs/seen.txt"    # in a subdirectory
```

then the old code created `./seen_es.txt` (next to config.yaml), but the new
code creates `./jobs/seen_es.txt` (next to `seen.txt`). If you have per-language
seen files in the "wrong" directory from before the fix, **move them into the
correct subdirectory** — otherwise `mpt batch --lang es` won't find them and
will re-render videos it has already produced.

If your `seen_file` was `./seen.txt` (at the same level as config.yaml), nothing
changes.

---

## 6. Versions and tags

The monorepo starts at **v1.0.0**. Tags from the four repositories were
deliberately not imported: three of them independently tagged `v1.0.0`, and
mixing those histories under one tag namespace would make the tags mean nothing.

Per-tool release history stays in the archived repositories. What *was* preserved
is the commit history itself: `git log` and `git blame` on any file reach back
into the original repository's commits, with the original authors, dates, and
messages. `git log --follow` even chains through the pre-`src/` layouts.

```bash
git log --follow src/mpt_autopilot/pilot/refill.py    # back into shorts-pilot
git blame src/mpt_autopilot/batch/run.py              # original authorship
```

---

## 7. What to do with the old repositories

Archive them rather than deleting them — they hold the release tags and are the
only place your old configs still exist for comparison. Nothing in MPT Autopilot
reads them, and the merge left them completely untouched.

Uninstall the old commands so a stale crontab line fails loudly instead of
quietly running a version you no longer maintain.

---

## 8. Checklist

```
[ ] uv sync in the new repo
[ ] cp config.example.yaml config.yaml, move your four configs into its sections
[ ] reconcile langs: file_suffix if pilot and batch disagreed
[ ] check batch.seen_file is not in a subdirectory with orphaned seen_<suffix>.txt files
[ ] rename enricher.directory → enricher.videos_dir (if coming from hashtag-enricher)
[ ] set telegram_prefix if you had per-stage Telegram filters
[ ] cp .env.example .env, fill in LLM + Telegram credentials
[ ] cp accounts.example.yaml accounts.yaml, or move your old one next to config.yaml
[ ] move yt-uploader-ledger.sqlite next to config.yaml
[ ] point jobs_dir / seen_dir at your existing jobs and seen files
[ ] mpt run --dry-run          # verifies paths, languages, and accounts
[ ] update crontab to the mpt commands
[ ] archive the four old repositories
```

`mpt run --dry-run` is the fastest way to confirm the migration: it loads the
config, resolves every language, checks each uploader account, and reports what
each stage would do — without rendering, calling an LLM, or uploading.
