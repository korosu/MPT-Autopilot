# batch — `mpt batch`

[![CI](https://github.com/korosu/mpt-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/korosu/mpt-autopilot/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](../LICENSE)

Batch video generator for [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo).

Define a list of videos in a YAML file, run one command, walk away. Already-generated
videos are tracked and skipped automatically, so re-running after a crash or an
interrupted session is always safe.

This is the **batch** stage of MPT Autopilot (formerly the separate `mpt-batch`
tool). It reads the `batch:` section of the shared `config.yaml` and the shared
`langs:` / `paths:` blocks.

---

## Features

- **YAML job list** — define as many videos as you want, with per-job parameter overrides
- **Voice presets** — friendly aliases for `tts_server` + `voice_name`, pre-filled in `config.yaml` and ready to use
- **Resumable** — tracks finished videos in `seen.txt`, and re-attaches to a task that was still rendering when the previous run died
- **Retry logic** — configurable retries per job with a delay between attempts
- **Abort on API failure** — stops after N consecutive failures so you don't waste hours
- **Configurable cache cleanup** — periodically clears MoneyPrinterTurbo's `cache_videos/`, or disable it entirely
- **Telegram alerts** — optional notifications on start, finish, and abort
- **Multiple profiles** — `--lang` (or an explicit `--jobs`) runs different jobs files for different languages or accounts

---

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) — recommended runner (see below)
- [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) running and reachable

---

## Installation

One clone and one `uv sync` install all four stages:

```bash
git clone https://github.com/korosu/mpt-autopilot.git
cd mpt-autopilot
uv sync
cp .env.example .env
cp config.example.yaml config.yaml
cp jobs.example.yaml jobs.yaml
```

Telegram alerts are optional — open `.env` and leave both keys empty to disable them, or fill in:

```
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

Open `config.yaml` and set `batch.mpt_storage` to your MoneyPrinterTurbo storage path — the
`batch.voices:` section already ships with a ready-to-use Gemini set, and all 314 free Edge TTS
voices are built in, so there is no extra setup needed there. Open `jobs.yaml` and add your own
video topics.

---

## Running

### Recommended: uv

Modern Debian/Ubuntu systems restrict installing packages into the system Python directly
(you may see an `externally-managed-environment` error). The cleanest solution is
[uv](https://github.com/astral-sh/uv) — a fast Python runner that handles isolated
environments automatically, with no manual `pip install` needed.

**Install uv** (if you don't have it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Run** — `uv sync` already created the environment, and `uv run` uses it:

```bash
# Generate all pending jobs from jobs.yaml
mpt batch

# Use a different jobs file (e.g. for another language or account)
mpt batch --jobs jobs.yaml
mpt batch --jobs jobs_es.yaml

# Or let a configured language derive jobs file, seen file and output dir at once
mpt batch --lang es

# Use a different config — before or after the subcommand, both work
mpt --config /path/to/config.yaml batch --jobs jobs.yaml
mpt batch --config /path/to/config.yaml --jobs jobs.yaml

# Preview which jobs would run without generating anything
mpt batch --dry-run

# Show seen registry stats
mpt batch --status

# Browse/search available voice aliases (see Voices below)
mpt batch --list-voices es_
```

Every example also works as `uv run mpt batch ...` if you'd rather not rely on the
installed console script being on `PATH`.

### Flags

| Flag | Meaning |
| ---- | ------- |
| `--jobs PATH` | Jobs file path. Default: `batch.jobs` from `config.yaml`, else `jobs.yaml` (or `jobs_<suffix>.yaml` with `--lang`) inside `batch.jobs_dir` / `paths.jobs_dir`, else the current directory. |
| `--lang CODE` | A key from the shared `langs:` section. Derives the jobs file, the seen file, and the output directory from that language's `file_suffix` — `--lang es` → `jobs_es.yaml`, `seen_es.txt`, `exports_es/`. The derived seen file takes its *name* from `batch.seen_file` but is looked for **beside `config.yaml`**; pass `--seen` explicitly if yours lives in a subdirectory. |
| `--seen PATH` | Override `batch.seen_file` (e.g. `--seen seen_es.txt`). A relative path resolves against `config.yaml`'s directory. |
| `--dry-run` | Preview which jobs would run, and validate every voice alias, without generating anything. |
| `--status` | Print the seen registry path, its entry count, and every registered filename, then exit. |
| `--list-voices [FILTER]` | List voice aliases (all bundled Edge TTS voices plus your `batch.voices:` presets) and exit. Optional substring filter. |
| `--list-bgm [FILTER]` | List `.mp3` files in MoneyPrinterTurbo's `resource/songs/` and exit. Optional filename-substring filter. |
| `--upload-bgm [PATH]` | Copy `.mp3` files into MoneyPrinterTurbo's `resource/songs/` and exit. Default source: the current directory. |
| `--config PATH` | Config file path (default `./config.yaml`). Also accepted as `mpt --config PATH batch`; the one after the subcommand wins if you pass both. |

### Alternative: virtual environment

If you prefer not to use uv, create a venv manually:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

mpt batch
```

You'll need to activate the venv (`source .venv/bin/activate`) each time you open a new terminal.

---

## How it works

1. Reads the jobs file and checks each `output_file` against the seen registry
2. Skips jobs that are already registered or marked `enabled: false`
3. Submits remaining jobs to MoneyPrinterTurbo one at a time, polling until each finishes
4. Copies the finished video (plus `script.json` and any `.srt`/`.ass` subtitle files MoneyPrinterTurbo produced) to `batch.output_dir`
5. Appends the `output_file` to the seen registry immediately after each success

Before the first submission the MoneyPrinterTurbo API is health-checked; if it is
unreachable the run aborts without submitting anything and says so in a Telegram alert.

### Seen file

The seen file is a plain-text file — one filename per line — that tracks which videos have already been generated. It's append-only, so a crash mid-run never loses progress. Re-running the script picks up exactly where it left off.

Since the merge this is one shared implementation (`src/mpt_autopilot/seen.py`) used by every
stage, so `mpt refill` counting pending jobs and `mpt batch` recording finished ones are
reading and writing the same registry with the same semantics.

`seen.txt` is gitignored and lives only on your machine.

### Resuming an interrupted run

Each `task_id` is written to a sidecar registry (`seen.in_progress.txt`, next to the seen file)
as soon as MoneyPrinterTurbo accepts the job — before polling starts. If the run dies mid-poll
(Ctrl-C, a reboot, a dropped SSH session), the next run re-attaches to that existing task and
polls it to completion instead of submitting a duplicate. An entry naming a video that is
already in the seen registry, or that no longer exists in the jobs file, is simply dropped.

A lock file (`seen.lock`, also next to the seen file) prevents two batches from running against
the same registry at once. It is refreshed after every successful video and removed on exit,
including on Ctrl-C and — where the platform has it — SIGTERM. A lock older than 30 minutes is
treated as stale and overwritten; if you see the "another batch may be running" error and you are
sure nothing is, delete the file and re-run.

### Warnings you may see

The run checks the jobs file before doing any work and warns (without stopping) about:

- no `defaults:` section at all, or a `defaults:` with no `video_language` — the likely cause of a video rendering in the wrong language
- two enabled jobs sharing one `output_file` — the first wins and the later ones are skipped as "already done"

---

## Jobs file format

`defaults` accepts any MoneyPrinterTurbo `VideoParams` field — voice, video assembly, subtitles, background music, rendering. A minimal example:

```yaml
defaults:
  video_language: "en"
  voice: "gemini_puck"        # alias from config.yaml's batch.voices: section — see "Voices" below
  voice_rate: 1.1
  video_clip_duration: 4
  paragraph_number: 3

jobs:
  - name: "Morning Routine Tips"
    output_file: "morning_routine_tips.mp4"   # must be unique
    enabled: true
    video_subject: "5 morning routine tips for productivity"

  - name: "Sleep Better"
    output_file: "sleep_better.mp4"
    enabled: false    # skip this one for now
    video_subject: "How to fall asleep faster"

  # Per-job override — any field overrides defaults for this job only
  - name: "Consejos de productividad"
    output_file: "consejos_es.mp4"
    enabled: true
    video_subject: "5 consejos de productividad"
    video_language: "es"
    voice: "es_es_elvira"
```

`name`, `enabled`, `output_file`, `max_retries`, and `retry_delay_seconds` are the batch stage's
own bookkeeping fields and are never sent to MoneyPrinterTurbo. Everything else in a job (merged
over `defaults`) goes straight into the API payload.

Per-job `max_retries` / `retry_delay_seconds` override the global values from `config.yaml` for
that one job:

```yaml
  - name: "Long Render"
    output_file: "long_render.mp4"
    video_subject: "A topic that takes a while"
    max_retries: 5
    retry_delay_seconds: 300
```

[`jobs.example.yaml`](../jobs.example.yaml) lists every field (script/subject, video assembly, subtitles, background music, rendering) with the values MoneyPrinterTurbo itself defaults to, so you have one place to see everything that's tunable.

---

## Voices

MoneyPrinterTurbo selects the voice purely from the shape of `voice_name` — a `"provider:voice"` prefix for paid providers, or a plain Edge TTS voice ID (e.g. `es-ES-ElviraNeural`) for the free default:

| Provider | Example `voice_name` | Cost |
|---|---|---|
| Edge TTS ("Azure TTS V1" in the WebUI) | `es-ES-ElviraNeural-Female` | Free, no key |
| Gemini TTS | `gemini:puck` | Paid, needs Gemini API key on the server |
| SiliconFlow / MiMo / ElevenLabs / Azure TTS V2 | see MoneyPrinterTurbo's `voice.py` | Paid, needs their own key on the server |

The batch stage bundles **all 314 Edge TTS voices** (every language MoneyPrinterTurbo's Edge TTS list covers) as ready-to-use aliases — nothing to configure. Browse or search them:

```bash
mpt batch --list-voices          # all 314+ voices
mpt batch --list-voices es_      # just the es_* locales (Spain, Mexico, Argentina, ...)
mpt batch --list-voices gemini   # your config.yaml presets
```

```
$ mpt batch --list-voices es_es
3 voice(s) matching 'es_es':

  es_es_alvaro                             voice_name=es-ES-AlvaroNeural-Male
  es_es_elvira                             voice_name=es-ES-ElviraNeural-Female
  es_es_ximena                             voice_name=es-ES-XimenaNeural-Female

Use in jobs.yaml as:  voice: "<alias>"
```

The filter matches the alias *or* the underlying `voice_name`, so `--list-voices elvira` and
`--list-voices es-ES` both find the same entries.

Use any alias directly in `jobs.yaml` — no `config.yaml` editing required:

```yaml
defaults:
  voice: "es_es_elvira"   # es-ES-ElviraNeural (Female)

jobs:
  - name: "Consejos de productividad"
    output_file: "consejos_productividad.mp4"
    video_language: "es"
    voice: "es_mx_dalia"   # overrides just for this job
```

`config.yaml`'s `batch.voices:` section is for **extra** presets on top of the bundled ones: paid providers (ships with a working Gemini set), or a bundled Edge voice with a custom rate/volume:

```yaml
# config.yaml
batch:
  voices:
    gemini:
      gemini_puck:
        tts_server: "gemini"
        voice_name: "gemini:puck"

    # Override a bundled voice's pace — same alias name wins over the built-in one
    edge_overrides:
      es_es_elvira:
        tts_server: "edge"
        voice_name: "es-ES-ElviraNeural-Female"
        voice_rate: 0.9
```

The grouping keys (`gemini`, `edge_overrides`, …) are for readability only. Alias names must be
unique across the whole section — a duplicate is a hard error — and a preset may only set
`tts_server`, `voice_name`, `voice_rate`, and `voice_volume`; anything else is rejected with the
alias named in the message.

`tts_server` / `voice_name` still work directly in a job if you'd rather skip aliases entirely — `voice` is purely a convenience that resolves to both fields before the job is submitted. `--dry-run` validates every alias up front, so a typo shows up immediately instead of failing mid-batch.

**Paid providers need their own setup on the MoneyPrinterTurbo server itself** — a Gemini API key, for example, goes in *MoneyPrinterTurbo's* own config, not in MPT Autopilot. This stage only resolves the alias to `tts_server` / `voice_name`; it has no way to configure or verify the MoneyPrinterTurbo server's TTS backend.

---

## Telegram alerts

Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Alerts are sent when a batch starts, finishes, and on abort. Leave both empty to disable.

This is a one-way notifier — `src/mpt_autopilot/notify.py` pushes plain `sendMessage` calls to the Telegram Bot API directly, the same way a `curl` command would. It doesn't listen for commands and isn't tied to any interactive bot project; if you also run a Telegram bot for VPS management, the two are independent and can share the same token without conflicting.

Since the merge every stage uses that same notifier, so one token and one chat cover the whole
pipeline. The prefix shown in each alert comes from `config.yaml` — the shared top-level value,
which any section may override:

```yaml
telegram_prefix: "mpt-autopilot"   # shared default → "[mpt-autopilot] Batch done"

batch:
  telegram_prefix: "my-server"     # only the batch stage's alerts
```

---

## Where batch sits in the pipeline

`mpt refill` fills the jobs queue, `mpt batch` renders it, `mpt enrich` tags the results, and
`mpt upload` publishes them:

```
mpt refill ──→ jobs.yaml / jobs_<suffix>.yaml ──→ mpt batch ──→ exports/ (video + .json)
     ↑                                                 │                        │
     └──── seen.txt / seen_<suffix>.txt ←──────────────┘         mpt enrich ──→ mpt upload
              (refill reads it, batch appends to it)
```

`mpt run` does all four for each configured language — see [pipeline.md](pipeline.md).

**One-time setup:**

1. Your jobs file must have a `defaults:` section with at least `video_language`:
   ```yaml
   defaults:
     video_language: "en"   # REQUIRED: otherwise wrong language may be used
     video_aspect: "9:16"
     subtitle_enabled: true
   jobs:
     # …
   ```

2. For multi-language, define the language in the shared `langs:` block and use `--lang`:
   ```bash
   mpt refill --lang en           # writes jobs.yaml
   mpt refill --lang es           # writes jobs_es.yaml
   mpt batch --lang en            # renders jobs.yaml    → seen.txt,    exports/
   mpt batch --lang es            # renders jobs_es.yaml → seen_es.txt, exports_es/
   ```

   `--jobs` / `--seen` still work if you want to point at files by hand:
   ```bash
   mpt batch --jobs jobs.yaml --seen seen.txt
   mpt batch --jobs jobs_es.yaml --seen seen_es.txt
   ```

Without `--lang` or `--seen`, the batch stage uses `batch.seen_file` from `config.yaml` — videos from other languages may be re-generated.

---

## Configuration

There is now **one** `config.yaml` for all four stages, split into sections. Copy
`config.example.yaml` to `config.yaml` and edit — every setting is documented inline:

```yaml
telegram_prefix: "mpt-autopilot"

paths:
  jobs_dir: "./jobs"
  exports_dir: "./exports"

langs:
  en:
    file_suffix: ""          # jobs.yaml,    seen.txt,    exports/
  es:
    file_suffix: "_es"       # jobs_es.yaml, seen_es.txt, exports_es/

batch:
  api_url: "http://127.0.0.1:8080"
  mpt_storage: "/root/MoneyPrinterTurbo/storage"
  mpt_songs_dir: "/root/MoneyPrinterTurbo/resource/songs"
  seen_file: "./seen.txt"
  max_retries: 3
  max_consecutive_failures: 3
```

The batch stage only reads `batch:` plus the shared top-level blocks. It never reads
`pilot:`, `enricher:`, or `uploader:`, so you can leave sections you don't use out entirely.

| Key | Default | Meaning |
| --- | ------- | ------- |
| `batch.api_url` | *required* | MoneyPrinterTurbo API endpoint. A value not starting with `http` is warned about. |
| `batch.mpt_storage` | *required* | MoneyPrinterTurbo's `storage/` directory, as the machine running it sees it (holds `tasks/` and `cache_videos/`). A missing directory is warned about at startup. |
| `batch.mpt_songs_dir` | *required* | MoneyPrinterTurbo's `resource/songs/` directory — see [Background music](#background-music). |
| `batch.output_dir` | `paths.exports_dir`, else `./exports` | Where finished videos and their `script.json` land. `--lang` appends the language's `file_suffix`. |
| `batch.seen_file` | `./seen.txt` | The dedup registry. |
| `batch.jobs` | — | A direct jobs file path. When set it wins over `--lang` suffixes and `jobs_dir`. |
| `batch.jobs_dir` | `paths.jobs_dir` | Directory the default `jobs<suffix>.yaml` is looked for in. |
| `batch.log_file` | `./logs/batch.log` | One backup copy (`batch.log.1`) is kept on rotation. |
| `batch.log_max_mb` | `10` | Rotation threshold. |
| `batch.max_wait_seconds` | `2400` | Give up waiting for one task after this long. |
| `batch.stuck_threshold_seconds` | `3600` | Give up if a task's progress does not move for this long. |
| `batch.max_retries` | `3` | Attempts per job. Overridable per job. |
| `batch.retry_delay_seconds` | `180` | Pause between attempts. Overridable per job. |
| `batch.max_consecutive_failures` | `3` | Circuit breaker: abort the whole batch after this many failures in a row. |
| `batch.cache_cleanup_enabled` | `true` | See [Cache cleanup](#cache-cleanup). |
| `batch.cache_cleanup_interval` | `6` | Clean every N successful videos; `0` = only at the end. |
| `batch.voices` | `{}` | Extra voice presets on top of the bundled Edge TTS pool. |
| `batch.telegram_prefix` | top-level `telegram_prefix` | Alert prefix for this stage only. |

**Every relative path in `config.yaml` resolves against `config.yaml`'s own directory**, never
the directory you run `mpt` from. That is what makes a cron entry behave identically no matter
where it starts:

```bash
mpt --config /srv/mpt/config.yaml batch    # ./exports means /srv/mpt/exports
```

Retries cover transient trouble — a dropped connection, MoneyPrinterTurbo briefly down, a
one-off stuck task. They will not fix bad credentials or a genuinely dead API; that is what
`max_consecutive_failures` is for, and hitting it aborts the run with a Telegram alert naming
how far it got.

---

## Cache cleanup

MoneyPrinterTurbo accumulates stock footage in `cache_videos/` as it generates videos, which can grow large over a long batch. The batch stage can clear it for you:

```yaml
batch:
  cache_cleanup_enabled: true     # set to false to never touch cache_videos/
  cache_cleanup_interval: 6       # also clean every N successful videos
```

Cleanup runs once at the end of a batch (when enabled, and unless a periodic run already cleaned after the last successful video). `cache_cleanup_interval` additionally triggers it periodically during a long run so disk usage doesn't grow unbounded; set it to `0` to only clean at the very end.

Each finished task's own directory under `storage/tasks/` is removed as soon as its video has
been copied out, independently of this setting.

---

## Background music

MoneyPrinterTurbo can mix background music into videos. Set `batch.mpt_songs_dir` to
MPT's real `resource/songs/` directory; it is separate from `batch.mpt_storage`.
The batch stage uses that directory to discover and manage BGM files.

### List available BGM

```bash
# List all .mp3 files in MPT's resource/songs/
mpt batch --list-bgm

# Filter by filename substring
mpt batch --list-bgm calm
```

```
$ mpt batch --list-bgm
3 BGM file(s):

  ambient_calm.mp3              2048 KB
  upbeat_energy.mp3             1536 KB
  lo-fi_chill.mp3               3072 KB

Use in jobs.yaml as:  bgm_type: "custom"  bgm_file: "<name>"  bgm_volume: 0.2
```

### Upload your own BGM

Copy `.mp3` files from a local directory into MPT's `resource/songs/`:

```bash
# Upload all .mp3 files from current directory
mpt batch --upload-bgm

# Upload from a specific directory
mpt batch --upload-bgm ~/my_background_music/
```

### Use BGM in jobs.yaml

Once files are in `resource/songs/`, reference them in your jobs:

```yaml
defaults:
  bgm_type: "custom"       # "random" | "builtin" | "custom" | "none"
  bgm_file: "ambient_calm.mp3"
  bgm_volume: 0.2          # 0.0–1.0

jobs:
  - name: "Relaxing Morning Routine"
    output_file: "morning_routine.mp4"
    video_subject: "5 morning routine tips for productivity"
    bgm_type: "custom"
    bgm_file: "lo-fi_chill.mp3"
    bgm_volume: 0.15
```

`bgm_type: "random"` picks a random file from `resource/songs/`; `"none"` disables
background music entirely.

---

## Running on a schedule (cron)

```bash
# Render every night at 20:30
30 20 * * * cd /srv/mpt && /usr/local/bin/mpt --config /srv/mpt/config.yaml batch >> logs/cron.log 2>&1
```

If you also refill, enrich, and upload on a schedule, one `mpt run` line replaces four —
see [pipeline.md](pipeline.md).

---

## Updating

```bash
cd mpt-autopilot && git pull && uv sync
```

Your `config.yaml`, `jobs.yaml`, and `seen.txt` are gitignored and will not be affected.

---

## Related

- [pilot.md](pilot.md) — `mpt refill`, `mpt init-seen` (fills the jobs queue batch renders)
- [enricher.md](enricher.md) — `mpt enrich` (reads what batch writes)
- [uploader.md](uploader.md) — `mpt upload`
- [pipeline.md](pipeline.md) — `mpt run`, all four stages per language
- [migration.md](migration.md) — coming from the separate `mpt-batch` tool

---

## Third-party notices

This project mentions [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) for integration purposes only.
This reference is purely descriptive. **This project is not affiliated with, sponsored by,
or endorsed by MoneyPrinterTurbo, and it does not constitute an endorsement of MPT Autopilot.**
Use of third-party tools is at your own risk — please review their respective licenses and documentation independently.

---

## License

This project is licensed under the MIT License. See the [LICENSE](../LICENSE) file for details.
