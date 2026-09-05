# pilot — `mpt refill` and `mpt init-seen`

[![CI](https://github.com/korosu/mpt-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/korosu/mpt-autopilot/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](../LICENSE)

Auto-generate YouTube Shorts video ideas and keep your [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) jobs queue filled via LLM.

Point it at your `jobs.yaml` or `jobs_<suffix>.yaml` file — it checks how many videos are still
pending, calls an LLM when the queue runs low, and appends fresh ideas in the
correct format automatically.

This is the **pilot** stage of MPT Autopilot (formerly the separate `shorts-pilot`
tool). It reads the `pilot:` section of the shared `config.yaml` and the shared
`langs:` / `paths:` blocks.

---

## Features

- **Queue-aware** — only generates new ideas when your pending count drops below a configurable threshold
- **Deduplication built-in** — tracks every generated video in `seen.txt`, never repeats a topic
- **Provider-agnostic** — works with OpenAI, Groq, Together, Mistral, Ollama, Anthropic — just set `LLM_BASE_URL`
- **Multi-language** — generates English, Spanish, or any language you define in the shared `langs:` block
- **Topics mode** — import topics verbatim (no LLM) with `--topic` or `--topics`
- **Theme mode** — constrain LLM to specific themes (e.g., "job", "animal") via a language's `theme_list`

---

## Where pilot sits in the pipeline

`mpt refill` generates job entries; `mpt batch` renders the videos; `mpt enrich` tags them; `mpt upload` publishes them:

```
mpt refill ──→ jobs.yaml / jobs_<suffix>.yaml ──→ mpt batch ──→ exports/ (video + .json)
     ↑                                                 │                        │
     └──── seen.txt / seen_<suffix>.txt ←──────────────┘         mpt enrich ──→ mpt upload
              (refill reads it, batch appends to it)
```

`mpt run` does all four for each configured language — see [pipeline.md](pipeline.md).

**One-time setup for multi-language:**

The `file_suffix` field of a language in the shared `langs:` block controls both jobs and seen filenames:

| `file_suffix` | Jobs file | Seen file | Exports dir |
| ------------- | --------- | --------- | ----------- |
| `""` (empty) | `jobs.yaml` | `seen.txt` | `exports/` |
| `"_es"` | `jobs_es.yaml` | `seen_es.txt` | `exports_es/` |

1. Create the appropriate jobs file with a `defaults:` section (required):
   ```yaml
   defaults:
     video_language: "en"
     video_aspect: "9:16"
     subtitle_enabled: true
   jobs:
     # …
   ```

2. After running `mpt refill --lang es`, render with the matching language:
   ```
   mpt refill --lang en --jobs-dir /path/to/jobs  # writes to jobs.yaml + reads seen.txt
   mpt refill --lang es --jobs-dir /path/to/jobs  # writes to jobs_es.yaml + reads seen_es.txt
   mpt batch --lang en                            # jobs.yaml    → seen.txt,    exports/
   mpt batch --lang es                            # jobs_es.yaml → seen_es.txt, exports_es/
   ```

   `mpt batch --jobs jobs_es.yaml --seen seen_es.txt` is the explicit equivalent if you prefer
   naming both files by hand.

`defaults:` must contain `video_language` at minimum — without it, the batch stage may render videos in the wrong language or aspect ratio, and it warns when the key is missing.

The jobs file must already exist: `mpt refill` appends to it and never creates it, and the error
it raises when the file is missing prints the minimal `defaults:` / `jobs:` skeleton to start
from. If you have a `jobs_en.yaml` from before (a language whose `file_suffix` is `""`), it is
still read, with a `[migration]` notice suggesting you rename it to `jobs.yaml`.

Because `langs:` is now shared at the top level of `config.yaml` rather than duplicated per tool,
pilot and batch can no longer disagree about what `_es` means.

---

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) — recommended runner (see below)
- An API key for any OpenAI-compatible LLM provider (or Anthropic)

---

## Installation

One clone and one `uv sync` install all four stages:

```
git clone https://github.com/korosu/mpt-autopilot.git
cd mpt-autopilot
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

`mpt refill` requires all three. `.env` is looked for next to `config.yaml` first, then in the
current directory, so `mpt --config /srv/mpt/config.yaml refill --lang en` picks up
`/srv/mpt/.env` automatically.

Open `config.yaml` and adjust thresholds, voices, and scan paths to your setup.
Your `config.yaml` is gitignored — `git pull` will never overwrite your settings.

---

## Running

`mpt` is installed by `uv sync`, so it can be run from anywhere as long as `--config` points at
your config file (or you are in the directory that holds it). Every example below also works as
`uv run mpt ...`.

```
# Refill English jobs (triggers when fewer than 10 ideas are pending)
mpt refill --lang en --jobs-dir /your/path/to/jobs

# Refill Spanish
mpt refill --lang es --jobs-dir /your/path/to/jobs

# Force a refill even if the queue is full
mpt refill --lang en --jobs-dir /your/path/to/jobs --force

# Generate exactly 50 ideas in one LLM call (no threshold top-up)
mpt refill --lang en --jobs-dir /your/path/to/jobs --count 50

# A config file somewhere else entirely — before or after the subcommand, both work
mpt --config /srv/mpt/config.yaml refill --lang en
mpt refill --config /srv/mpt/config.yaml --lang en
```

### Commands

**`mpt refill`** — the main command that populates the jobs queue:

| Flag | Meaning |
| ---- | ------- |
| `--lang LANG` | Required. Language code (e.g. `en`, `es`). Must be defined in `config.yaml`'s shared `langs:` block. |
| `--jobs-dir PATH` | Directory containing `jobs.yaml` / `jobs_<suffix>.yaml`. Default: `pilot.jobs_dir`, else `paths.jobs_dir`, else current directory. |
| `--seen-dir PATH` | Directory for `seen*.txt`. Default: `pilot.seen_dir`, else `paths.seen_dir`, else `--jobs-dir`. |
| `--force` | Refill even if the queue is already full (pending ≥ threshold). |
| `--count N` | Generate exactly N ideas in one LLM call — skips threshold top-up entirely. Must be positive. |
| `--threshold N` | Override `pilot.generation.threshold` from `config.yaml`. Must be non-negative. |
| `--topic "TEXT"` (repeatable) | Import a specific topic as a job (no LLM). Combined with `--topics`. |
| `--topics FILE` | UTF-8 file with topics (one per line, blank lines ignored) imported as jobs. No LLM. |
| `--theme THEME` (repeatable) | Constrain the LLM to a configured theme. Requires that theme in the language's `theme_list`. |
| `--config PATH` | Config file path (default `./config.yaml`). Also accepted as `mpt --config PATH refill`. |

`--topic`/`--topics` cannot be combined with `--theme` — one bypasses the LLM, the other drives
it — and `--count` / `--threshold` / `--force` are ignored in topics mode (the run says so).

**`mpt init-seen`** — catalog existing videos so they won't be regenerated:

| Flag | Meaning |
| ---- | ------- |
| `--dir PATH` (repeatable) | Directory to scan for `.mp4` files. Combined with `pilot.scan_dirs` from `config.yaml`; at least one source must exist between the two. |
| `--lang LANG` | Filter by that language's `file_suffix` and write to `seen_<suffix>.txt`. Omit to register all files into `seen.txt`. |
| `--seen-dir PATH` | Directory for `seen*.txt` files. Default: `pilot.seen_dir`, else `paths.seen_dir`, else current directory. |
| `--config PATH` | Config file path (default `./config.yaml`). |

### Topics mode (no LLM)

Import specific topics as jobs without LLM — the topic text becomes `video_subject` verbatim:

```
# Single topic
mpt refill --lang en --topic "The tongue is not the strongest muscle in your body"

# Multiple topics from a file (one per line)
mpt refill --lang en --topics topics.txt
```

Topic lines are automatically stripped of common list markers (`1.`, `-`, `*`, `•`):

```
# Input file topics.txt:
1. Octopuses have three hearts
- Penguins propose with a stone
2) Giraffes sleep the least
5 mistakes everyone makes at work  # content number preserved
```

Topics mode does not need LLM credentials at all — `LLM_API_KEY` and friends may be empty.

### Theme mode (LLM constrained to themes)

`theme_list` lives **inside a language**, under the shared `langs:` block:

```yaml
langs:
  en:
    label: English
    file_suffix: ""
    theme_list:
      - job
      - animal
      - computer
```

Then use them with `--theme` (or run without to use all of that language's configured themes):

```
# Use all configured themes
mpt refill --lang en --force --count 20

# Use only specific theme(s)
mpt refill --lang en --theme job --theme animal --force --count 5
```

A `--theme` value that isn't in the language's `theme_list` is an error naming the configured
themes, rather than a silently ignored flag. In theme mode the LLM returns short topic *titles*
(3–12 words) rather than full 3–5 sentence subjects, so the minimum-length check on
`video_subject` is relaxed accordingly.

`--jobs-dir` / `--seen-dir` can be skipped once you set `paths.jobs_dir`
(and optionally `paths.seen_dir`) in `config.yaml`:

```yaml
paths:
  jobs_dir: /your/path/to/jobs
  seen_dir: /your/path/to/jobs   # optional, defaults to jobs_dir
```

With that in place, `mpt refill --lang en` is enough. An explicit `--jobs-dir`
on the command line always takes priority over `config.yaml`, and a `pilot.jobs_dir` /
`pilot.seen_dir` inside the `pilot:` section takes priority over the shared `paths:` block.

### Alternative: virtual environment

```
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install .

mpt refill --lang en --jobs-dir /your/path/to/jobs
```

---

## How it works

1. Reads `jobs.yaml` or `jobs_<suffix>.yaml` and counts **pending** jobs — those that are `enabled: true` and whose `output_file` is not yet in the seen file
2. If pending is below the threshold — or `--force` / an explicit `--count` bypasses that check — calls the LLM for new ideas. If the queue is still under threshold after dedup, makes up to 2 additional calls to top up, stopping early if a top-up produces nothing new. `--count N` skips the top-up and generates exactly N jobs in one call.
3. Deduplicates against the seen file and what's already in the yaml
4. Appends new job entries to the jobs yaml in the correct format

Appends are strictly additive: existing lines are never rewritten or reformatted, and the new
content is swapped in atomically, so a concurrent reader can never see a half-written file. A
lock is held across the read-and-replace cycle, so two refills (two languages, or a retry racing
a previous run) cannot lose each other's jobs.

Everything the LLM returns is validated against the language's config before it is written:
`voice_name` must be one of the configured `voices`, `voice_rate` must sit between
`voice_rate_min` and `voice_rate_max`, and `video_concat_mode` / `bgm_type` must be values
MoneyPrinterTurbo recognises. Anything missing, mistyped, or out of range is replaced with the
configured default rather than written through, and a job with no usable `video_subject` is
skipped with a `[skip]` line instead of taking the batch down.

The prompt's "already used topics" list covers both rendered videos (from the seen file) **and**
topics already queued in the jobs yaml but not yet rendered, so a refill can't propose
near-duplicates of a pending backlog.

`mpt refill` only writes to the jobs yaml. The seen file is updated by `mpt batch`, after each
video is actually rendered.

### Seen file

The seen file is a plain-text file (one filename per line) that tracks which videos have already been generated. Its name is determined by the language's `file_suffix`:

| `file_suffix`         | seen file     |
| --------------------- | ------------- |
| `""` (empty, default) | `seen.txt`    |
| `"_es"`               | `seen_es.txt` |
| `"_en"`               | `seen_en.txt` |

Since the merge this is one shared implementation (`src/mpt_autopilot/seen.py`) used by every
stage, so pilot counting pending jobs and batch recording finished ones read and write the same
registry with the same semantics.

---

## Registering existing videos

If you already have generated videos, run `mpt init-seen` to scan your folders
and register them so they won't be generated again. Safe to run multiple times.

```
mpt init-seen --dir /your/path/to/videos

# Multiple directories
mpt init-seen --dir /your/path/to/videos --dir /your/path/to/videos/old
```

**Multi-language setups** — use `--lang` to filter by suffix and write to separate seen files:

```
# English: registers only files without a known lang suffix → seen.txt
mpt init-seen --lang en --dir /your/path/to/videos

# Spanish: registers only files ending with _es.mp4 → seen_es.txt
mpt init-seen --lang es --dir /your/path/to/videos
```

The filter uses the full set of `file_suffix` values from `langs:`, so the empty-suffix language
keeps exactly the files that end in none of the others — no guessing.

**Override seen directory:**

```
mpt init-seen --dir /your/videos --seen-dir /your/seen/files
```

You can also define permanent scan paths in `config.yaml` under `pilot.scan_dirs` so you don't need to pass `--dir` every time:

```yml
pilot:
  scan_dirs: [/your/path/to/videos,
              /your/path/to/videos/en,
              /your/path/to/videos/old_videos,
              /your/path/to/videos/en/old_videos]
```

Then just run:

```
mpt init-seen
```

`--dir` values are *added* to `pilot.scan_dirs` rather than replacing them; a directory that
does not exist is reported as `[skip]` and the rest are still scanned.

---

## Configuration

There is now **one** `config.yaml` for all four stages, split into sections. The pilot stage
reads `pilot:` plus the shared `langs:` and `paths:` blocks — and note that `langs:` sits at the
**top level**, not under `pilot:`, because batch and `mpt run` need the same answer:

```yml
paths:
  jobs_dir: /your/path/to/jobs
  seen_dir: /your/path/to/jobs   # optional, defaults to jobs_dir

pilot:
  generation:
    count: 21       # how many ideas to generate per refill
    threshold: 10   # refill when pending jobs drop below this

  # Permanent directories to scan when running `mpt init-seen`
  scan_dirs:
    - /your/path/to/videos

langs:
  en:
    label: English
    file_suffix: ""        # empty → jobs.yaml + seen.txt
    voice_rate_min: 1.05
    voice_rate_max: 1.20
    voices:
      - gemini:puck
      - gemini:orus
      # ... (all 8 voices listed in config.example.yaml)

    # Optional: constrain the LLM to specific themes for this language
    theme_list:
      - job
      - animal
      - computer

    job_defaults:
      video_clip_duration: 3
      video_concat_mode: random
      bgm_type: random
      bgm_volume: 0.15
      paragraph_number: 2
      duration_range: "30-60"   # narration target (optional)

  es:
    label: Spanish
    file_suffix: "_es"     # → jobs_es.yaml + seen_es.txt
    job_defaults:
      video_clip_duration: 4
```

| Key | Default | Meaning |
| --- | ------- | ------- |
| `pilot.generation.count` | `21` | Ideas per refill. Overridden by `--count`. |
| `pilot.generation.threshold` | `10` | Refill only fires below this many pending jobs. Overridden by `--threshold`; ignored by `--force`. |
| `pilot.generation.reasoning_enabled` | *omitted* | Three-state: omit the key entirely and nothing extra is sent (identical to a plain OpenAI-compatible provider); `true`/`false` explicitly opts in or out. Only relevant for self-hosted vLLM / SGLang / NVIDIA NIM backends that read `chat_template_kwargs.reasoning_effort`. |
| `pilot.generation.reasoning_effort` | `medium` | `none`\|`low`\|`medium`\|`high`\|`xhigh`\|`max`, sent only when `reasoning_enabled: true`. |
| `pilot.generation.reasoning_max_tokens` | `8192` | Extra budget *added on top of* the per-job token estimate, reserved for the hidden reasoning draft. |
| `pilot.scan_dirs` | `[]` | Directories `mpt init-seen` scans, combined with any `--dir`. |
| `pilot.jobs_dir` / `pilot.seen_dir` | `paths.*` | Stage-level overrides of the shared `paths:` block. |
| `langs.<code>.label` | `<CODE>` | Language name used in the prompt ("in Spanish"). |
| `langs.<code>.file_suffix` | `_<code>` | Derives `jobs`, `seen`, and `exports` names. |
| `langs.<code>.voice_rate_min` / `_max` | `1.05` / `1.20` | Accepted range for the LLM's `voice_rate`; out-of-range values fall back to the minimum. |
| `langs.<code>.voices` | `[]` | The `voice_name` values the LLM may pick from. A value outside the list is replaced with the first entry. |
| `langs.<code>.theme_list` | `[]` | Optional theme mode; empty means free-topic generation. |
| `langs.<code>.job_defaults` | `{}` | Written into every generated job entry. |

If a reasoning model burns its whole budget on the hidden draft and returns empty content, the
error message says so and points at these three keys — that is the failure they exist for.

**Every relative path in `config.yaml` resolves against `config.yaml`'s own directory**, never
the directory you run `mpt` from, so a cron entry behaves identically wherever it starts.

### `duration_range`

`job_defaults.duration_range` is a narration target in seconds — `"30-60"` for a range, `"120+"`
for a floor with no ceiling. It is converted to word bounds and injected into the generated
job's `video_script_prompt`, and it may nudge `paragraph_number` **up** (never down) so a longer
script has somewhere to go. An invalid value is rejected before any LLM call is made, naming the
expected format. Omit the key for no length constraint.

Every generated job gets a hook instruction in `video_script_prompt` whether or not
`duration_range` is set: the first sentence must grab the viewer in 3–5 seconds, with no
greetings or throat-clearing.

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
  video_script_prompt: "Open with a single-sentence hook in the first 3–5 seconds of voiceover: ..."
```

Only these keys are written. Anything else the LLM invents is dropped with a `[note]` line, so a
hallucinated field can never reach MoneyPrinterTurbo. `output_file` is lowercased and stripped of
any path components, and given the language's `file_suffix` if the LLM forgot it (or stripped of
another language's suffix if it used the wrong one) — that suffix is what `mpt init-seen` and
`mpt batch --lang` filter on later.

---

## Updating

```
cd mpt-autopilot && git pull && uv sync
```

Your `config.yaml`, `jobs.yaml`, and `seen.txt` are gitignored and will not be affected.

---

## Related

- [batch.md](batch.md) — `mpt batch` (renders the queue pilot fills)
- [enricher.md](enricher.md) — `mpt enrich`
- [uploader.md](uploader.md) — `mpt upload`
- [pipeline.md](pipeline.md) — `mpt run`, all four stages per language
- [migration.md](migration.md) — coming from the separate `shorts-pilot` tool

---

## Third-party notices

This project mentions [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) for integration purposes only.
This reference is purely descriptive. **This project is not affiliated with, sponsored by,
or endorsed by MoneyPrinterTurbo, and it does not constitute an endorsement of MPT Autopilot.** Use of third-party tools is at your own risk — please review their respective licenses and documentation independently.

---

## License

MIT License. See [LICENSE](../LICENSE) for details.
