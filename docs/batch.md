# mpt-batch

[![lint](https://github.com/korosu/mpt-batch/actions/workflows/lint.yml/badge.svg)](https://github.com/korosu/mpt-batch/actions/workflows/lint.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Batch video generator for [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo).

Define a list of videos in a YAML file, run one command, walk away. Already-generated
videos are tracked and skipped automatically, so re-running after a crash or an
interrupted session is always safe.

---

## Features

- **YAML job list** — define as many videos as you want, with per-job parameter overrides
- **Voice presets** — name your favorite voices once in `voices.yaml`, reference them by alias
- **Resumable** — tracks finished videos in `seen.txt`; safe to re-run after interruption
- **Retry logic** — configurable retries per job with a delay between attempts
- **Abort on API failure** — stops after N consecutive failures so you don't waste hours
- **Configurable cache cleanup** — periodically clears MoneyPrinterTurbo's `cache_videos/`, or disable it entirely
- **Telegram alerts** — optional notifications on start, finish, and abort
- **Multiple profiles** — run with different jobs files for different languages or accounts

---

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) — recommended runner (see below)
- [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) running and reachable

---

## Installation

```bash
git clone https://github.com/korosu/mpt-batch.git
cd mpt-batch
cp .env.example .env
cp config.yaml.example config.yaml
cp jobs.example.yaml jobs.yaml
cp voices.example.yaml voices.yaml   # optional — see "Voices" below
```

Telegram alerts are optional — open `.env` and leave it empty to disable them, or fill in:

```
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

Open `config.yaml` and set `mpt_storage` to your MoneyPrinterTurbo storage path. Open `jobs.yaml` and add your own video topics.

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

**Run** — uv installs dependencies into an isolated environment on the first run:

```bash
# Generate all pending jobs from jobs.yaml
uv run batch

# Use a different jobs file (e.g. for another language or account)
uv run batch --jobs jobs_en.yaml
uv run batch --jobs jobs_es.yaml

# Use a different config
uv run batch --config /path/to/config.yaml --jobs jobs_en.yaml

# Preview which jobs would run without generating anything
uv run batch --dry-run

# Show seen registry stats
uv run batch --status
```

### Alternative: virtual environment

If you prefer not to use uv, create a venv manually:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

batch
```

You'll need to activate the venv (`source .venv/bin/activate`) each time you open a new terminal.

---

## How it works

1. Reads `jobs.yaml` and checks each `output_file` against `seen.txt`
2. Skips jobs that are already in `seen.txt` or marked `enabled: false`
3. Submits remaining jobs to MoneyPrinterTurbo one at a time, polling until each finishes
4. Copies the finished video (and `script.json`, if MoneyPrinterTurbo produced one) to `output_dir`
5. Appends the `output_file` to `seen.txt` immediately after each success

### Seen file

The seen file is a plain-text file — one filename per line — that tracks which videos have already been generated. It's append-only, so a crash mid-run never loses progress. Re-running the script picks up exactly where it left off.

`seen.txt` is gitignored and lives only on your machine.

---

## Jobs file format

`defaults` accepts any MoneyPrinterTurbo `VideoParams` field — voice, video assembly, subtitles, background music, rendering. A minimal example:

```yaml
defaults:
  video_language: "en"
  voice: "gemini_puck"        # resolved via voices.yaml — see "Voices" below
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
    voice: "gtts_es"
```

[`jobs.example.yaml`](jobs.example.yaml) lists every field (script/subject, video assembly, subtitles, background music, rendering) with the values MoneyPrinterTurbo itself defaults to, so you have one place to see everything that's tunable.

---

## Voices

Your MoneyPrinterTurbo setup selects the TTS engine via a `tts_server` field, and `voice_name` itself encodes the provider as a `"provider:voice"` prefix. Each provider names its voices completely differently:

| Provider | `tts_server` | Example `voice_name` | Cost |
|---|---|---|---|
| gTTS (Google Translate TTS) | `gtts` | `gtts:en` | Free, no key |
| Gemini TTS | `gemini` | `gemini:puck` | Paid, needs Gemini API key on the server |

(Other providers your MoneyPrinterTurbo fork supports follow the same `tts_server` + `"provider:voice"` shape — check its `voice.py` for the exact values it expects.)

Remembering both fields by hand for every job gets old fast, so mpt-batch supports named aliases via `voices.yaml`, each resolving to **both** fields together so they can never drift out of sync:

```yaml
# voices.yaml
gemini:
  gemini_puck:
    tts_server: "gemini"
    voice_name: "gemini:puck"
  gemini_aoede:
    tts_server: "gemini"
    voice_name: "gemini:aoede"
```

Then in `jobs.yaml`, reference the alias instead of setting both fields by hand:

```yaml
defaults:
  voice: "gemini_puck"

jobs:
  - name: "Focus Hacks"
    output_file: "focus_hacks.mp4"
    voice: "gemini_aoede"   # overrides tts_server + voice_name together, just for this job
```

`tts_server` / `voice_name` still work directly if you'd rather skip presets entirely — `voice` is purely a convenience that resolves to both fields before the job is submitted. `--dry-run` validates every alias up front, so a typo shows up immediately instead of failing mid-batch.

A starter pool with confirmed voices for gTTS and Gemini is in [`voices.example.yaml`](voices.example.yaml).

**Paid providers need their own setup on the MoneyPrinterTurbo server itself** — a Gemini API key, for example, goes in *MoneyPrinterTurbo's* own config, not in mpt-batch. This tool only resolves the alias to `tts_server` / `voice_name`; it has no way to configure or verify the MoneyPrinterTurbo server's TTS backend.

---

## Multiple languages / accounts

Create a separate jobs file per language and run them independently:

```bash
uv run batch --jobs jobs_en.yaml
uv run batch --jobs jobs_es.yaml
```

Both runs share the same `seen.txt` by default, so there's no risk of one
run re-generating a video the other already produced.

---

## Telegram alerts

Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Alerts are sent when a batch starts, finishes, and on abort. Leave both empty to disable.

This is a one-way notifier — mpt-batch pushes plain `sendMessage` calls to the Telegram Bot API directly, the same way a `curl` command would. It doesn't listen for commands and isn't tied to any interactive bot project; if you also run a Telegram bot for VPS management, the two are independent and can share the same token without conflicting.

The prefix shown in every alert (`[mpt-batch] Batch done`) can be changed in `config.yaml`:

```yaml
telegram_prefix: "my-server"
```

---

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit as needed — every setting is documented inline:

```yaml
api_url: "http://127.0.0.1:8080"
mpt_storage: "/root/MoneyPrinterTurbo/storage"
output_dir: "./exports"
seen_file: "./seen.txt"
max_retries: 3
max_consecutive_failures: 3
```

---

## Cache cleanup

MoneyPrinterTurbo accumulates stock footage in `cache_videos/` as it generates videos, which can grow large over a long batch. mpt-batch can clear it for you:

```yaml
cache_cleanup_enabled: true     # set to false to never touch cache_videos/
cache_cleanup_interval: 6       # also clean every N successful videos
```

Cleanup always runs once at the end of a batch (when enabled). `cache_cleanup_interval` additionally triggers it periodically during a long run so disk usage doesn't grow unbounded; set it to `0` to only clean at the very end.

---

## Running on a schedule (cron)

```bash
# Generate every night at 20:30
30 20 * * * cd /root/mpt-batch && uv run batch --jobs jobs_en.yaml >> logs/cron.log 2>&1
```

---

## Updating

```bash
cd mpt-batch && git pull
```

Your `config.yaml`, `jobs.yaml`, `voices.yaml`, and `seen.txt` are gitignored and will not be affected.

---

## Third-party notices

This project mentions [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) for integration purposes only.
This reference is purely descriptive. **This project is not affiliated with, sponsored by,
or endorsed by MoneyPrinterTurbo, and it does not constitute an endorsement of mpt-batch.**
Use of third-party tools is at your own risk — please review their respective licenses and documentation independently.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
