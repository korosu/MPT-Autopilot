![Python Version](https://img.shields.io/badge/python-%3E%3D3.10-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

# MPT Autopilot

End-to-end automation for [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo):
generate short-video ideas, render them, tag them, and upload them to YouTube —
one repository, one command, one config file.

```bash
mpt run
```

That chains four stages, once per language:

```
refill  →  batch  →  enrich  →  upload
```

| Stage | Command | What it does |
|---|---|---|
| **pilot** | `mpt refill` | asks an LLM for fresh video ideas and appends them to the jobs queue, deduplicated against everything you've already made |
| **batch** | `mpt batch` | renders pending jobs through the MoneyPrinterTurbo API, with retries, timeout handling, and cache cleanup |
| **enricher** | `mpt enrich` | generates platform-appropriate hashtags for each rendered video and writes them into its metadata sidecar |
| **uploader** | `mpt upload` | uploads to YouTube via [youtubeuploader](https://github.com/porjo/youtubeuploader), multi-account, with a crash-safe SQLite ledger |

Every stage also runs on its own, so you can adopt as much or as little of the
chain as you want.

> **Coming from the four separate tools?**
> This repository merges [shorts-pilot](https://github.com/korosu/shorts-pilot),
> [mpt-batch](https://github.com/korosu/mpt-batch),
> [hashtag-enricher](https://github.com/korosu/hashtag-enricher), and
> [yt-shorts-uploader](https://github.com/korosu/yt-shorts-uploader).
> [**docs/migration.md**](docs/migration.md) has the full mapping and a checklist.

## Requirements

- Python 3.10+ and [uv](https://docs.astral.sh/uv/)
- A running [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)
  instance (for the batch stage)
- An OpenAI-compatible LLM endpoint — OpenAI, Anthropic, Groq, Together, or a
  local Ollama / vLLM (for refill and enrich)
- [youtubeuploader](https://github.com/porjo/youtubeuploader) and a Google Cloud
  OAuth client (for the upload stage)

You only need the pieces for the stages you actually use.

## Install

```bash
git clone https://github.com/korosu/mpt-autopilot.git
cd mpt-autopilot
uv sync

cp config.example.yaml config.yaml
cp .env.example .env
cp jobs.example.yaml jobs.yaml
cp accounts.example.yaml accounts.yaml     # only for the upload stage
```

Check the wiring before anything runs for real:

```bash
uv run mpt run --dry-run
```

That loads the config, resolves every language, checks each uploader account, and
prints what each stage would do — without rendering a video, calling an LLM, or
uploading anything.

## Configuration

One `config.yaml`, one section per stage, plus a shared part every stage reads:

```yaml
telegram_prefix: "mpt-autopilot"

paths:
  jobs_dir: "./jobs"
  exports_dir: "./exports"

langs:                      # shared: one source of truth for every stage
  en:
    label: English
    file_suffix: ""         # jobs.yaml,    seen.txt,    exports/
  es:
    label: Spanish
    file_suffix: "_es"      # jobs_es.yaml, seen_es.txt, exports_es/

pilot:
  generation:
    count: 21
    threshold: 10

batch:
  api_url: "http://127.0.0.1:8080"
  mpt_storage: "/root/MoneyPrinterTurbo/storage"
  mpt_songs_dir: "/root/MoneyPrinterTurbo/resource/songs"

enricher:
  platform: youtube
  max_tags: 5

uploader:
  uploader_binary: "/root/youtubeuploader/youtubeuploader"

pipeline:
  accounts:
    es: spanish-channel     # only if your account isn't named after the language
```

[`config.example.yaml`](config.example.yaml) is the annotated version, with every
key and its default. Two things about it are worth knowing up front:

- **Every relative path resolves against `config.yaml`'s own directory**, never
  your current one. So cron can run `mpt --config /srv/mpt/config.yaml run` from
  anywhere and get identical behaviour.
- **`langs:` is shared, at the top level.** A language's `file_suffix` derives the
  jobs file, the seen registry, and the exports directory, which is exactly how
  the stages hand work to each other with no extra configuration.

Secrets — LLM keys and the optional Telegram bot token — go in `.env`, never in
`config.yaml`. `mpt` looks for `.env` next to `config.yaml` first, then in the
current directory.

## Usage

Per stage:

```bash
mpt refill --lang en                    # generate ideas via LLM
mpt refill --lang en --topic "Why the Moon has no atmosphere"   # skip the LLM
mpt init-seen --dir /videos/en          # register existing videos so refill won't repeat them

mpt batch --lang en                     # render pending jobs
mpt batch --dry-run                     # preview which jobs would run
mpt batch --status                      # seen-registry stats
mpt batch --list-voices es              # browse the 314 bundled Edge TTS voices

mpt enrich --dir ./exports_en           # generate hashtags
mpt enrich --file clip.mp4 --force      # re-generate for one file
mpt enrich --platform tiktok            # different tag limits and prompt

mpt upload --account en                 # upload one channel
mpt upload --all-accounts --limit 5     # every channel, 5 videos each
```

The whole chain:

```bash
mpt run                                 # every configured language
mpt run --lang en                       # one language
mpt run --only batch --only enrich      # a slice of the pipeline
mpt run --skip upload                   # produce everything, upload later
mpt run --continue-on-error             # don't stop a language on the first failure
```

`--config` works before or after the subcommand. Every subcommand has its own
`--help`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | succeeded, or nothing to do |
| 1 | failure |
| 2 | stopped on YouTube's daily upload quota |

A quota stop gets its own code so a scheduled wrapper can tell "try again
tomorrow" apart from "something is broken". `mpt run` aggregates across languages
with failure taking precedence over a quota stop.

### Scheduling

```cron
0 */6 * * * cd /srv/mpt && /usr/local/bin/mpt --config /srv/mpt/config.yaml run >> /srv/mpt/logs/run.log 2>&1
```

Optional Telegram alerts report each run's outcome — set `TELEGRAM_TOKEN` and
`TELEGRAM_CHAT_ID` in `.env`, or leave them empty to disable notifications.

## Documentation

| | |
|---|---|
| [docs/pilot.md](docs/pilot.md) | idea generation, themes, dedup, `mpt refill` / `mpt init-seen` |
| [docs/batch.md](docs/batch.md) | rendering, jobs.yaml, voices, retries, `mpt batch` |
| [docs/enricher.md](docs/enricher.md) | hashtag generation, prompts, platform limits, `mpt enrich` |
| [docs/uploader.md](docs/uploader.md) | accounts, sidecars, the ledger, `mpt upload` |
| [docs/pipeline.md](docs/pipeline.md) | `mpt run` — stage selection, failure handling, scheduling |

## Development

```bash
uv sync --extra dev
uv run ruff check . && uv run ruff format --check . && uv run pyright src && uv run pytest tests/ -v
```

CI runs exactly that on every push and pull request against `main`.

The layout mirrors the stages:

```
src/mpt_autopilot/
  cli.py  pipeline.py  config.py  seen.py  notify.py  lock.py  logger.py
  pilot/  batch/  enricher/  uploader/
```

The four stage packages never import each other — everything shared goes through
the top-level modules. [CLAUDE.md](CLAUDE.md) documents the stage contract and the
config invariants worth knowing before changing a settings module.

Commit history from all four original repositories is preserved, so `git blame`
and `git log --follow` reach back into each file's original project:

```bash
git log --follow src/mpt_autopilot/pilot/refill.py
```

## License

[MIT](LICENSE)
