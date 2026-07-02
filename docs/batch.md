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
- **Voice presets** — friendly aliases for `tts_server` + `voice_name`, pre-filled in `config.yaml` and ready to use
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
```

Telegram alerts are optional — open `.env` and leave it empty to disable them, or fill in:

```
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

Open `config.yaml` and set `mpt_storage` to your MoneyPrinterTurbo storage path — it also ships with a ready-to-use `voices:` section, no extra setup needed there. Open `jobs.yaml` and add your own video topics.

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

# Browse/search available voice aliases (see Voices below)
uv run batch --list-voices es_
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
  voice: "gemini_puck"        # alias from config.yaml's voices: section — see "Voices" below
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

MoneyPrinterTurbo selects the voice purely from the shape of `voice_name` — a `"provider:voice"` prefix for paid providers, or a plain Edge TTS voice ID (e.g. `es-ES-ElviraNeural`) for the free default:

| Provider | Example `voice_name` | Cost |
|---|---|---|
| Edge TTS ("Azure TTS V1" in the WebUI) | `es-ES-ElviraNeural-Female` | Free, no key |
| Gemini TTS | `gemini:puck` | Paid, needs Gemini API key on the server |
| SiliconFlow / MiMo / ElevenLabs / Azure TTS V2 | see MoneyPrinterTurbo's `voice.py` | Paid, needs their own key on the server |

mpt-batch bundles **all 314 Edge TTS voices** (every language MoneyPrinterTurbo's Edge TTS list covers) as ready-to-use aliases — nothing to configure. Browse or search them:

```bash
uv run batch --list-voices          # all 314+ voices
uv run batch --list-voices es_      # just the es_* locales (Spain, Mexico, Argentina, ...)
uv run batch --list-voices gemini   # your config.yaml presets
```

```
$ uv run batch --list-voices es_es
3 voice(s) matching 'es_es':

  es_es_alvaro                             voice_name=es-ES-AlvaroNeural-Male
  es_es_elvira                             voice_name=es-ES-ElviraNeural-Female
  es_es_ximena                             voice_name=es-ES-XimenaNeural-Female

Use in jobs.yaml as:  voice: "<alias>"
```

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

`config.yaml`'s `voices:` section is for **extra** presets on top of the bundled ones: paid providers (ships with a working Gemini set), or a bundled Edge voice with a custom rate/volume:

```yaml
# config.yaml
voices:
  gemini:
    gemini_puck:
      tts_server: "gemini"
      voice_name: "gemini:puck"

  # Override a bundled voice's pace — same alias name wins over the built-in one
  slower_spanish:
    es_es_elvira:
      tts_server: "edge"
      voice_name: "es-ES-ElviraNeural-Female"
      voice_rate: 0.9
```

`tts_server` / `voice_name` still work directly in a job if you'd rather skip aliases entirely — `voice` is purely a convenience that resolves to both fields before the job is submitted. `--dry-run` validates every alias up front, so a typo shows up immediately instead of failing mid-batch.

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

Your `config.yaml`, `jobs.yaml`, and `seen.txt` are gitignored and will not be affected.

---

## Third-party notices

This project mentions [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) for integration purposes only.
This reference is purely descriptive. **This project is not affiliated with, sponsored by,
or endorsed by MoneyPrinterTurbo, and it does not constitute an endorsement of mpt-batch.**
Use of third-party tools is at your own risk — please review their respective licenses and documentation independently.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
