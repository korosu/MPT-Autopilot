# shorts-pilot

[![lint](https://github.com/korosu/shorts-pilot/actions/workflows/lint.yml/badge.svg)](https://github.com/korosu/shorts-pilot/actions/workflows/lint.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Auto-generate YouTube Shorts video ideas and keep your [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) jobs queue filled via LLM.

Point it at your `jobs.yaml` or `jobs_<suffix>.yaml` file — it checks how many videos are still
pending, calls an LLM when the queue runs low, and appends fresh ideas in the
correct format automatically.

---

## Features

- **Queue-aware** — only generates new ideas when your pending count drops below a configurable threshold
- **Deduplication built-in** — tracks every generated video in `seen.txt`, never repeats a topic
- **Provider-agnostic** — works with OpenAI, Groq, Together, Mistral, Ollama, Anthropic — just set `LLM_BASE_URL`
- **Multi-language** — generates English, Spanish, or any language you define in `config.yaml`
- **Topics mode** — import topics verbatim (no LLM) with `--topic` or `--topics`
- **Theme mode** — constrain LLM to specific themes (e.g., "job", "animal") via `config.yaml` `theme_list`

---

## Works together with [mpt-batch](https://github.com/korosu/mpt-batch)

shorts-pilot generates job entries; mpt-batch renders the videos:

```
shorts-pilot ──→ jobs.yaml / jobs_<suffix>.yaml ──→ mpt-batch ──→ videos
     ↓                                                                     ↓
  seen.txt / seen_<suffix>.txt (tracks what's been generated) ←────────────
```

**One-time setup for multi-language:**

The `file_suffix` field in `config.yaml` controls both jobs and seen filenames:

| `file_suffix` | Jobs file | Seen file |
| ------------- | --------- | --------- |
| `""` (empty) | `jobs.yaml` | `seen.txt` |
| `"_es"` | `jobs_es.yaml` | `seen_es.txt` |

1. Create the appropriate jobs file with a `defaults:` section (required):
   ```yaml
   defaults:
     video_language: "en"
     video_aspect: "9:16"
     subtitle_enabled: true
   jobs:
     # …
   ```

2. After running `refill --lang es`, use matching seen file:
   ```
   uv run refill --lang en --jobs-dir /path/to/jobs  # writes to jobs.yaml + seen.txt
   uv run refill --lang es --jobs-dir /path/to/jobs  # writes to jobs_es.yaml + seen_es.txt
   uv run batch --jobs jobs.yaml --seen seen.txt
   uv run batch --jobs jobs_es.yaml --seen seen_es.txt
   ```

`defaults:` must contain `video_language` at minimum — without it, mpt-batch may render videos in the wrong language or aspect ratio.

---

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) — recommended runner (see below)
- An API key for any OpenAI-compatible LLM provider (or Anthropic)

---

## Installation

```
git clone https://github.com/korosu/shorts-pilot.git
cd shorts-pilot
cp .env.example .env
cp config.example.yaml config.yaml
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.11
uv sync
```

Open `.env` and fill in your API credentials:

```
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

Open `config.yaml` and adjust thresholds, voices, and scan paths to your setup.
Your `config.yaml` is gitignored — `git pull` will never overwrite your settings.

---

## Running

All commands must be run from the `shorts-pilot` directory:

```
# Refill English jobs (triggers when fewer than 10 ideas are pending)
uv run refill --lang en --jobs-dir /your/path/to/jobs

# Refill Spanish
uv run refill --lang es --jobs-dir /your/path/to/jobs

# Force a refill even if the queue is full
uv run refill --lang en --jobs-dir /your/path/to/jobs --force

# Generate exactly 50 ideas in one LLM call (no threshold top-up)
uv run refill --lang en --jobs-dir /your/path/to/jobs --count 50
```

### Commands

**refill** — the main command that populates the jobs queue:

| Flag | Meaning |
| ---- | ------- |
| `--lang LANG` | Required. Language code (e.g. `en`, `es`). Must be defined in `config.yaml`. |
| `--jobs-dir PATH` | Directory containing `jobs.yaml` / `jobs_<suffix>.yaml`. Default: `paths.jobs_dir` in `config.yaml`, or current directory. |
| `--seen-dir PATH` | Directory for `seen_<lang>.txt`. Default: `paths.seen_dir` in `config.yaml`, or `--jobs-dir`. |
| `--force` | Refill even if the queue is already full (pending ≥ threshold). |
| `--count N` | Generate exactly N ideas in one LLM call — skips threshold top-up entirely. |
| `--threshold N` | Override `generation.threshold` from `config.yaml`. |
| `--topic "TEXT"` (repeatable) | Import a specific topic as a job (no LLM). Combined with `--topics`. |
| `--topics FILE` | File with topics (one per line) imported as jobs. No LLM. |
| `--theme THEME` (repeatable) | Constrain LLM to a configured theme. Requires `theme_list` in `config.yaml`. |

**init-seen** — catalog existing videos so they won't be regenerated:

| Flag | Meaning |
| ---- | ------- |
| `--dir PATH` (repeatable) | Required: directory to scan for `.mp4` files. |
| `--lang LANG` | Filter by suffix and write to `seen_<suffix>.txt`. Omit to register all files into `seen.txt`. |
| `--seen-dir PATH` | Directory for `seen_*.txt` files. Default: `paths.seen_dir` in `config.yaml`, or current directory. |

### Topics mode (no LLM)

Import specific topics as jobs without LLM — the topic text becomes `video_subject` verbatim:

```
# Single topic
uv run refill --lang en --topic "The tongue is not the strongest muscle in your body"

# Multiple topics from a file (one per line)
uv run refill --lang en --topics topics.txt
```

Topic lines are automatically stripped of common list markers (`1.`, `-`, `*`, `•`):

```
# Input file topics.txt:
1. Octopuses have three hearts
- Penguins propose with a stone
2) Giraffes sleep the least
5 mistakes everyone makes at work  # content number preserved
```

### Theme mode (LLM constrained to themes)

Add themes to `config.yaml` under `theme_list`:

```yaml
theme_list:
  - job
  - animal
  - computer
```

Then use them with `--theme` (or run without to use all configured themes):

```
# Use all configured themes
uv run refill --lang en --force --count 20

# Use only specific theme(s)
uv run refill --lang en --theme job --theme animal --force --count 5
```

`--jobs-dir` / `--seen-dir` can be skipped once you set `paths.jobs_dir`
(and optionally `paths.seen_dir`) in `config.yaml`:

```yaml
paths:
  jobs_dir: /your/path/to/jobs
  seen_dir: /your/path/to/jobs   # optional, defaults to jobs_dir
```

With that in place, `refill --lang en` is enough. An explicit `--jobs-dir`
on the command line always takes priority over `config.yaml`.

### Alternative: virtual environment

```
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install .

refill --lang en --jobs-dir /your/path/to/jobs
```

---

## How it works

1. Reads `jobs.yaml` or `jobs_<suffix>.yaml` and counts **pending** jobs — those that are `enabled: true` and whose `output_file` is not yet in the seen file
2. If pending >= threshold (and `--count` not passed), calls the LLM for new ideas. If the queue is still under threshold after dedup, makes up to 2 additional calls to top up. `--count N` skips this top-up and generates exactly N jobs in one call.
3. Deduplicates against the seen file and what's already in the yaml
4. Appends new job entries to the jobs yaml in the correct format


### Seen file

The seen file is a plain-text file (one filename per line) that tracks which videos have already been generated. Its name is determined by `file_suffix` in `config.yaml`:

| `file_suffix`         | seen file     |
| --------------------- | ------------- |
| `""` (empty, default) | `seen.txt`    |
| `"_es"`               | `seen_es.txt` |
| `"_en"`               | `seen_en.txt` |

---

## Registering existing videos

If you already have generated videos, run `init-seen` to scan your folders
and register them so they won't be generated again. Safe to run multiple times.

```
uv run init-seen --dir /your/path/to/videos

# Multiple directories
uv run init-seen --dir /your/path/to/videos --dir /your/path/to/videos/old
```

**Multi-language setups** — use `--lang` to filter by suffix and write to separate seen files:

```
# English: registers only files without a lang suffix → seen.txt
uv run init-seen --lang en --dir /your/path/to/videos

# Spanish: registers only files ending with _es.mp4 → seen_es.txt
uv run init-seen --lang es --dir /your/path/to/videos
```

**Override seen directory:**

```
uv run init-seen --dir /your/videos --seen-dir /your/seen/files
```

You can also define permanent scan paths in `config.yaml` under `scan_dirs` so you don't need to pass `--dir` every time:

```yml
scan_dirs: [/your/path/to/videos,
            /your/path/to/videos/en,
            /your/path/to/videos/old_videos,
            /your/path/to/videos/en/old_videos]
```

Then just run:

```
uv run init-seen
```

---

## Configuration

Edit `config.yaml` to adjust thresholds, voices, or add new languages:

```yml
generation:
  count: 21       # how many ideas to generate per refill
  threshold: 10   # refill when pending jobs drop below this

# Permanent directories to scan when running init-seen
scan_dirs:
  - /your/path/to/videos

# Optional: constrain LLM to specific themes
theme_list:
  - job
  - animal
  - computer

langs:
  en:
    label: English
    file_suffix: ""        # empty → uses seen.txt
    voices:
      - gemini:puck
      - gemini:orus
      # ... (all 8 voices listed in the default config)
    job_defaults:
      video_clip_duration: 3
      bgm_volume: 0.15
      paragraph_number: 2
      duration_range: "30-60"   # narration target (optional)

  es:
    label: Spanish
    file_suffix: "_es"     # → jobs_es.yaml + seen_es.txt
    job_defaults:
      video_clip_duration: 4
```

## Output format

Each generated entry added to the jobs yaml looks like this:

```yml
- name: "fact_ants_outweigh_humans"
  enabled: true
  output_file: "fact_ants_outweigh_humans.mp4"
  video_subject: "There are roughly 20 quadrillion ants on Earth. If you weighed
    all of them together they would match the combined weight of all humans.
    Ants have colonized every continent except Antarctica. They just do not have
    social media."
  video_clip_duration: 3
  video_concat_mode: "random"
  voice_rate: 1.15
  voice_name: "gemini:orus"
  bgm_type: "random"
  bgm_volume: 0.15
  paragraph_number: 2
```

---

## Updating

```
cd shorts-pilot && git pull
```

Your `config.yaml` and `seen.txt` are gitignored and will not be affected.

---

## Third-party notices

This project mentions [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) for integration purposes only.
This reference is purely descriptive. **This project is not affiliated with, sponsored by,
or endorsed by MoneyPrinterTurbo, and it does not constitute an endorsement of shorts-pilot.** Use of third-party tools is at your own risk — please review their respective licenses and documentation independently.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
