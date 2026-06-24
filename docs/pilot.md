# shorts-pilot

Auto-generate YouTube Shorts video ideas and keep your [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) jobs queue filled via LLM.

Point it at your `jobs_<lang>.yaml` file — it checks how many videos are still
pending, calls an LLM when the queue runs low, and appends fresh ideas in the
correct format automatically.

---

## Features

- **Queue-aware** — only generates new ideas when your pending count drops below a configurable threshold
- **Deduplication built-in** — tracks every generated video in `seen.txt`, never repeats a topic
- **Provider-agnostic** — works with OpenAI, Groq, Together, Mistral, Ollama, Anthropic — just set `LLM_BASE_URL`
- **Multi-language** — generates English, Spanish, or any language you define in `config.yaml`

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
cp config.yaml.example config.yaml
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

# Generate 50 new ideas instead of the default 21
uv run refill --lang en --jobs-dir /your/path/to/jobs --count 50
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

1. Reads `jobs_<lang>.yaml` and counts **pending** jobs — those that are `enabled: true` and whose `output_file` is not yet in the seen file
2. If pending < threshold (default: 10), calls the LLM for new ideas
3. Deduplicates against the seen file and what's already in the yaml
4. Appends new job entries to `jobs_<lang>.yaml` in the correct format


### Seen file

The seen file is a plain-text file (one filename per line) that tracks which videos have already been generated. Its name is determined by `file_suffix` in `config.yaml`:

| `file_suffix`         | seen file     |
| --------------------- | ------------- |
| `""` (empty, default) | `seen.txt`    |
| `"_es"`               | `seen_es.txt` |
| `"_en"`               | `seen_en.txt` |


Most users keep everything in one `seen.txt`. Multi-language setups with different `file_suffix` values get separate files automatically.

---

## Registering existing videos

If you already have generated videos, run `init-seen` to scan your folders
and register them so they won't be generated again. Safe to run multiple times.

**Most users** — just point it at your videos folder, everything goes into `seen.txt`:

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

You can also define permanent scan paths in `config.yaml` under `scan_dirs` so you don't need to pass `--dir` every time:

```
scan_dirs:
  - /your/path/to/videos
  - /your/path/to/videos/old_videos
```

Then just run:

```
uv run init-seen
```

---

## Configuration

Edit `config.yaml` to adjust thresholds, voices, or add new languages:

```
generation:
  count: 21       # how many ideas to generate per refill
  threshold: 10   # refill when pending jobs drop below this

# Permanent directories to scan when running init-seen
scan_dirs:
  - /your/path/to/videos

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

  es:
    label: Spanish
    file_suffix: "_es"     # → uses seen_es.txt
    job_defaults:
      video_clip_duration: 4
```

## Output format

Each generated entry added to `jobs_<lang>.yaml` looks like this:

```
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