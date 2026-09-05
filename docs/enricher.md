# `mpt enrich` — the enricher stage

Generate relevant YouTube/TikTok/Instagram hashtags for your video files using an LLM API.

Point it at a folder of `.mp4` files — it figures out the topic from the filename,
calls an LLM, and saves the hashtags into a `.json` file next to each video.
No video generator or special toolchain required.

> This stage was the standalone `hashtag-enricher` tool before the merge. Its
> behaviour is unchanged; it is now reached through `mpt enrich` and configured
> from the `enricher:` section of the one shared `config.yaml`.

---

## Where it sits in the pipeline

```
mpt refill  →  mpt batch  →  mpt enrich  →  mpt upload
```

`mpt batch` renders pending jobs into the exports directory. `mpt enrich` writes
hashtags into the sidecar `.json` next to each rendered video. `mpt upload` reads
that same sidecar and puts the tags into the YouTube metadata. `mpt run` does all
four in order for every configured language, passing each language's exports
directory to `enrich` explicitly.

`mpt enrich` exits `0` when every file was enriched or skipped, and `1` if any
file errored — that is what `mpt run` aggregates.

---

## Features

- **Works with any mp4 file** — topic is read from the filename by default
- **Auto-detects language** — no need to specify it; detected per-file via LLM in the
  same API call that generates the hashtags (see [Language detection](#language-detection))
- **Platform-aware** — target YouTube, TikTok, or Instagram; tag counts and limits adjust
  automatically (see [Platforms](#platforms))
- **MoneyPrinterTurbo-aware** — if a `script.json` exists next to the video, the richer `video_subject` field is used automatically
- **Provider-agnostic** — works with OpenAI, Groq, Together, local Ollama, GitHub Models — just change `LLM_BASE_URL`
- **Safe by default** — never overwrites existing hashtags unless `--force` is passed
- **Previewable** — `--dry-run` lists what it would do without spending a single token

---

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) — recommended runner (see below)
- An API key for any OpenAI-compatible LLM provider

---

## Installation

The whole suite installs once, from the repository root:

```bash
git clone https://github.com/korosu/mpt-autopilot.git
cd mpt-autopilot
uv sync
cp .env.example .env
cp config.example.yaml config.yaml
```

Open `.env` and add your API key:

```
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

Only `LLM_API_KEY` is strictly required by this stage; `LLM_BASE_URL` defaults to
`https://api.openai.com/v1` and `LLM_MODEL` to `gpt-4o-mini`. `LLM_TIMEOUT`
(seconds, default `60`) raises the read timeout for slow self-hosted backends.

Edit the `enricher:` section of `config.yaml` if you want to adjust the target
platform, tag limits, or always-included tags (the defaults work fine out of the
box). See [Configuration](#configuration) below for the full list of options.

`.env` is looked for next to `config.yaml` first, then in the current directory,
so `mpt --config /srv/mpt/config.yaml enrich` picks up `/srv/mpt/.env`
automatically.

---

## Running

`uv sync` puts a single `mpt` command on the path; `uv run mpt ...` works too and
needs no activated environment.

```bash
# Scan the configured videos_dir, or the current directory — language auto-detected per file
mpt enrich

# Scan a specific folder
mpt enrich --dir /home/user/videos

# Process a single file
mpt enrich --file /home/user/videos/my_clip.mp4

# Force a specific language for all files (skips LLM language detection → faster)
mpt enrich --dir /home/user/videos --lang Spanish
mpt enrich --dir /home/user/videos --lang en        # short codes work too

# Target a specific platform (overrides the enricher.platform setting)
mpt enrich --dir /home/user/videos --platform tiktok
mpt enrich --dir /home/user/videos --platform instagram

# Re-generate hashtags even if they already exist
mpt enrich --dir /home/user/videos --force

# See what would happen — no LLM calls, nothing written
mpt enrich --dir /home/user/videos --dry-run
```

`--dir` and `--file` are mutually exclusive. With neither, the stage scans
`enricher.videos_dir` from `config.yaml`, falling back to the current directory.
Scanning is **non-recursive**: only `*.mp4` files directly inside the directory
are processed, and a hint is logged if mp4s were found in subdirectories.

### Flags

| Flag | Effect |
|---|---|
| `--dir PATH` | Directory to scan for `*.mp4` files (default: `enricher.videos_dir`, else the current directory) |
| `--file FILE` | Process a single mp4 file |
| `--lang LANGUAGE` | Force one language for every file, e.g. `English`, `Spanish`, `ru`. Skips LLM language detection |
| `--platform PLATFORM` | `youtube`, `tiktok`, or `instagram`. Overrides `enricher.platform` for this run |
| `--force` | Re-generate hashtags even if the sidecar already has them |
| `--dry-run` | List the candidate files and what would happen to each, then exit |
| `--config PATH` | Which `config.yaml` to read (default `./config.yaml`) |

`--config` works both before and after the subcommand, so
`mpt --config /srv/mpt/config.yaml enrich` and
`mpt enrich --config /srv/mpt/config.yaml` are equivalent.

### `--dry-run`

`--dry-run` resolves the file list, the effective platform, and the tag range
exactly as a real run does, then reports its verdict per file and stops. It makes
**no LLM calls and writes nothing**:

```
[2026-06-30 14:02:09] [INFO] === enrich --dry-run: 3 file(s) | platform=youtube | tags=3–5 ===
[2026-06-30 14:02:09] [INFO]   ostriches_myth.mp4: would generate (no sidecar yet)
[2026-06-30 14:02:09] [INFO]   sound_in_space.mp4: would skip (already enriched)
[2026-06-30 14:02:09] [INFO]   opossum_faints.mp4: would generate (sidecar has no hashtags)
[2026-06-30 14:02:09] [INFO] Dry run — no LLM calls, no files written.
```

An unreadable sidecar reports `would re-generate (sidecar unreadable)`. With
`--force`, every file reports `would re-generate`, because that is what `--force`
makes a real run do.

A dry run still loads the full `enricher:` settings, so it needs `LLM_API_KEY` to
be present and it enforces the same `max_tags`/platform budget check — which is
exactly what makes it a useful config smoke test. It just never sends a request.

### Alternative: plain virtual environment

If you prefer not to use uv:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

mpt enrich --dir /home/user/videos
```

You'll need to activate the venv (`source .venv/bin/activate`) each time you open a new terminal.

---

## Platforms

Each platform has its own tag count limit:

| `--platform` value | Limit |
|---|---|
| `youtube` (default) | 60 |
| `tiktok` | 5 |
| `instagram` | 5 |

Select the platform either in `config.yaml` (`enricher.platform: tiktok`) or
per-run with `--platform tiktok`, which overrides the config file for that
invocation only — this changes both the prompt sent to the LLM and the hard limit
tags are truncated to, not just the `platform` field recorded in the output JSON.

Before processing any files, the tool checks that `enricher.max_tags` plus
`enricher.always_include` actually fits the effective platform's limit, and exits
with an error if it doesn't — e.g. the default `max_tags: 5` plus the default
`always_include: ["#shorts"]` is 6 tags, which doesn't fit tiktok/instagram's
limit of 5. Lower `max_tags` or trim `always_include` to fix it. The check runs
twice for a reason: once at startup against `enricher.platform`, and again for a
`--platform` override, so pointing a youtube-shaped config at a stricter platform
fails fast for the whole run instead of silently truncating every file.

---

## Language detection

The enricher figures out the language for each video in one of two ways:

- **You already know it** — pass `--lang Spanish` (or a short code like `--lang es`),
  or have a `video_language` field in an existing `script.json`. In this case the
  language detector is **never called** — the tool makes a single API call straight
  to hashtag generation in that language.
- **You don't know it / it varies per file** — leave `--lang` unset. The tool makes
  a single combined API call that detects the language *and* generates the hashtags
  at the same time, rather than two separate calls. This keeps cost and latency the
  same as the explicit-language path above, just with the language inferred instead
  of supplied.

Only if that combined call fails to parse does the tool fall back to two calls,
using `enricher.prompt_detect_language` first and then
`enricher.prompt_generate`.

`--lang` beats `script.json`, and common short codes (`en`, `es`, `ru`, `de`,
`fr`, `pt`, `it`, `zh`, `ja`, `ko`, `ar`, `hi`, `tr`, `pl`, `nl`) are expanded to
full language names before they reach the prompt. Anything unrecognised is passed
through verbatim, so `--lang Catalan` works too.

Note that `mpt run` deliberately does **not** pass `--lang`: the language codes in
`config.yaml`'s `langs:` block are file-suffix keys, not LLM language names, so
each file is detected on its own. Run `mpt enrich --lang ...` directly if you want
to pin it.

---

## Output

For each `*.mp4` file, a `{video_name}.json` is created (or updated) next to it:

```json
{
  "hashtags": {
    "tags_list": ["#shorts", "#romanempire", "#historyfacts", "#ancientrome"],
    "tags_string": "#shorts #romanempire #historyfacts #ancientrome",
    "tag_count": 4,
    "platform": "youtube",
    "generated_at": "2026-06-26T14:00:00Z",
    "model": "gpt-4o-mini",
    "detected_language": "English",
    "source": "filename"
  },
  "tags": ["#shorts", "#romanempire", "#historyfacts", "#ancientrome"]
}
```

`source` is `"filename"` when the topic came from the mp4 filename,
or `"script_json"` when it came from a `video_subject` field in an existing `.json` file.

`platform` reflects whichever platform was active for that run
(`enricher.platform`, or `--platform` if passed).

If a `.json` already existed, the `hashtags` key is **merged in** — all other
fields are preserved unchanged. The write is atomic (temp file plus rename), so a
crash or Ctrl-C never leaves a truncated sidecar behind.

Progress and results are also written to the log (`logs/enricher.log`, from
`enricher.log_dir`) and echoed to the terminal as each file finishes, e.g.:

```
[2026-06-30 14:02:11] [INFO] ok: my_clip.mp4 → #shorts #romanempire #historyfacts #ancientrome (4 tags, lang=English [detected], platform=youtube, source=filename)
```

Each run ends with a one-line tally:

```
Done. ok=12  skipped=3  error=0
```

---

## MoneyPrinterTurbo integration

If you use [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo),
a `script.json` is automatically placed next to each generated video — which is
exactly what `mpt batch` leaves in the exports directory.

The enricher detects this file and uses the `params.video_subject` field as the
topic — giving the LLM better context than just the filename. It also picks up
`params.video_language` as the language hint. This requires no configuration; it
happens automatically.

---

## Handoff to the upload stage

`mpt upload` reads the flat `"tags"` key from `{video_name}.json` and places
those hashtags into the YouTube upload metadata. Where they land is controlled by
`uploader.defaults.hashtag_placement` in `config.yaml`: `tags` (hidden metadata
only), `description` (visible hashtags), or `both` (default). See
[uploader.md](uploader.md).

The sidecar is the only contract between the two stages — the same file works if
you drive [youtubeuploader](https://github.com/porjo/youtubeuploader) yourself
instead of through `mpt upload`.

---

## Configuration

Everything lives under the `enricher:` section of the one shared `config.yaml`.
Copy `config.example.yaml` to `config.yaml` and edit as needed:

```yaml
enricher:
  # videos_dir: "./exports"  # scanned when --dir/--file is not given

  platform: youtube          # youtube | tiktok | instagram

  min_tags: 3
  max_tags: 5

  max_tag_length: 20         # tags longer than this (after the #) are dropped

  banned_tags: []            # empty by default — add your own, e.g. ["#viral"]

  always_include:
    - "#shorts"

  log_dir: "./logs"
  log_max_mb: 5

  supports_temperature: true # set false for models that reject `temperature`

  reasoning_enabled: false
  # reasoning_effort: "medium"
  # reasoning_max_tokens: 8192

  prompt_detect_language: |
    ...
  prompt_detect_and_generate: |
    ...
  prompt_generate: |
    ...
```

- **`videos_dir`** — the directory scanned when neither `--dir` nor `--file` is
  given. Omit it to default to the current directory. `mpt run` ignores it and
  passes the language's exports directory explicitly.
- **`platform`** — which platform's limits to enforce (`youtube`, `tiktok`, or
  `instagram`). Can be overridden per-run with `--platform`. See [Platforms](#platforms).
- **`min_tags` / `max_tags`** — how many hashtags the LLM should generate, not
  counting `always_include`. `min_tags` must be at least 1 and strictly less than
  `max_tags`. `max_tags` plus the number of `always_include` tags must together
  stay within the chosen platform's hard limit, or the tool will refuse to start
  (checked for both `enricher.platform` and any `--platform` override).
- **`max_tag_length`** — hashtags longer than this (counting only the part after
  `#`) are filtered out after generation. Minimum 2.
- **`banned_tags`** — hashtags that are always excluded from the output, no matter
  what the LLM generates. **Empty by default** — this is a blank list for you to
  fill in with whatever you personally don't want; the tool doesn't impose an
  opinion here.
- **`always_include`** — tags always prepended to every result, regardless of
  language, in the order listed. These count toward the platform's hard limit
  alongside `max_tags` (see above), even though they don't count toward
  `min_tags`/`max_tags` themselves. They are also injected into the prompt as
  "tags to exclude", so the LLM never duplicates them.
- **`log_dir` / `log_max_mb`** — where `enricher.log` is written and the size at
  which it rotates (3 backups kept).
- **`supports_temperature`** — set to `false` for reasoning models (o1, o3,
  o4-mini) that reject a `temperature` parameter.
- **`reasoning_enabled` / `reasoning_effort` / `reasoning_max_tokens`** —
  three-state opt-in for the vLLM/SGLang/NVIDIA-NIM `chat_template_kwargs`
  extension. Omit `reasoning_enabled` entirely and nothing is sent, which is what
  plain OpenAI-compatible providers want. `false` explicitly forces thinking off
  (for models that default it on and would otherwise spend the whole token budget
  on a hidden draft and return empty content). `true` enables it at
  `reasoning_effort` (`none`, `low`, `medium`, `high`, `xhigh`, `max`) and
  switches the request's token budget to `reasoning_max_tokens`. The same switch
  exists under `pilot.generation`.

You can also edit the `prompt_detect_language`, `prompt_detect_and_generate`, and
`prompt_generate` templates directly if you want to change the generation
strategy itself. All three are **required** — the stage refuses to start if any is
missing, rather than silently falling back to a built-in prompt. The placeholders
available to them are documented in `config.example.yaml`.

Every relative path in this section (`videos_dir`, `log_dir`) resolves against
`config.yaml`'s own directory, never the working directory — so a cron entry
behaves identically no matter where it starts.

---

## Troubleshooting

**`Missing required environment variable: LLM_API_KEY`** — `.env` was not found or
does not set the key. It is looked for next to `config.yaml` first, then in the
current directory.

**`Model returned empty content (finish_reason=length)`** — a reasoning model spent
the whole budget thinking. Raise `enricher.reasoning_max_tokens` or lower
`enricher.reasoning_effort`.

**`max_tags (5) + always_include (1 tag(s)) = 6 exceeds the tiktok limit of 5`** —
lower `enricher.max_tags`, trim `always_include`, or target a different platform.

**Everything is skipped** — the sidecars already contain a `hashtags` key. Use
`--force` to regenerate, and `--dry-run --force` first if you want to see the
blast radius.

**Nothing is found** — scanning is non-recursive. Check the hint line in the log:
it reports how many mp4s were found in subdirectories and ignored.

**Rate limits and gateway blips** — 429s (honouring `Retry-After`), 404/500/502/503/504,
and network timeouts are retried twice with exponential backoff before the file is
marked as an error. Response bodies are logged locally only, never included in the
Telegram alert.

---

## Notifications

Telegram alerts fire when the run starts, when it finishes, and on a per-file
error. Credentials go in `.env`:

```
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Leave both empty to disable notifications entirely. The prefix on each message
comes from `config.yaml`'s shared `telegram_prefix` (default `mpt-autopilot`);
add `telegram_prefix` inside the `enricher:` section to override it for this
stage only. The notifier itself is the shared
`src/mpt_autopilot/notify.py` used by every stage.

---

## Updating

```bash
cd mpt-autopilot && git pull && uv sync
```

`config.yaml`, `.env`, and `accounts.yaml` are gitignored, so a pull never
overwrites your settings.

---

## Third-party notices

This project mentions [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)
and [youtubeuploader](https://github.com/porjo/youtubeuploader) for integration purposes only.
These references are purely descriptive. **This project is not affiliated with, sponsored by,
or endorsed by either project, and neither project constitutes an endorsement of MPT Autopilot.**
Use of third-party tools is at your own risk — please review their respective licenses,
terms, and documentation independently.

---

## Where the code lives

```
src/mpt_autopilot/enricher/
  run.py          # flags (add_arguments) and the per-file loop
  settings.py     # the enricher: section, as a lazy settings singleton
  llm.py          # httpx client, retries, prompt formatting, response parsing
  postprocess.py  # length/banned/dedup filters and the platform hard limits
  reader.py       # topic + language hint from script.json or the filename
  writer.py       # atomic merge into {video_name}.json
```

`notify.py` (Telegram) and `logger.py` are shared top-level modules, not
stage-local copies. Note that the settings module is `settings.py` — it was
`config.py` in the standalone tool, renamed because `config.py` is now the shared
loader at `src/mpt_autopilot/config.py`.

---

## Related

- [pipeline.md](pipeline.md) — `mpt run`, the whole chain
- [batch.md](batch.md) — `mpt batch`, which produces the videos this stage tags
- [uploader.md](uploader.md) — `mpt upload`, which consumes the sidecars
- [migration.md](migration.md) — coming from the standalone `hashtag-enricher`

---

## License

This project is licensed under the MIT License. See the [LICENSE](../LICENSE) file for details.
