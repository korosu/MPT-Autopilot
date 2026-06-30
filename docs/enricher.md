# hashtag-enricher

[![lint](https://github.com/korosu/hashtag-enricher/actions/workflows/lint.yml/badge.svg)](https://github.com/korosu/hashtag-enricher/actions/workflows/lint.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Generate relevant YouTube/TikTok/Instagram hashtags for your video files using an LLM API.

Point it at a folder of `.mp4` files — it figures out the topic from the filename,
calls an LLM, and saves the hashtags into a `.json` file next to each video.
No video generator or special toolchain required.

---

## Features

- **Works with any mp4 file** — topic is read from the filename by default
- **Auto-detects language** — no need to specify it; detected per-file via LLM in the
  same API call that generates the hashtags (see [Language detection](#language-detection))
- **Platform-aware** — target YouTube, TikTok, or Instagram; tag counts and limits adjust
  automatically (see [Platforms](#platforms))
- **Research-backed defaults** — generates 3–5 hashtags per video by default, matching
  2025–2026 best practices and each platform's actual hard limits
- **Optional MoneyPrinterTurbo integration** — if a `script.json` exists next to the video, the richer `video_subject` field is used automatically
- **Provider-agnostic** — works with OpenAI, Groq, Together, local Ollama, GitHub Models — just change `LLM_BASE_URL`
- **Safe by default** — never overwrites existing hashtags unless `--force` is passed

---

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) — recommended runner (see below)
- An API key for any OpenAI-compatible LLM provider

---

## Installation

```bash
git clone https://github.com/korosu/hashtag-enricher.git
cd hashtag-enricher
cp .env.example .env
cp config.yaml.example config.yaml
```

Open `.env` and add your API key:

```
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

Edit `config.yaml` if you want to adjust the target platform, tag limits, or
always-included tags (the defaults work fine out of the box). See
[Configuration](#configuration) below for the full list of options.

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
# Scan current directory — language auto-detected per file
uv run enrich

# Scan a specific folder
uv run enrich --dir /home/user/videos

# Process a single file
uv run enrich --file /home/user/videos/my_clip.mp4

# Force a specific language for all files (skips LLM language detection → faster)
uv run enrich --dir /home/user/videos --lang Spanish
uv run enrich --dir /home/user/videos --lang en        # short codes work too

# Target a specific platform (overrides config.yaml's `platform` setting)
uv run enrich --dir /home/user/videos --platform tiktok
uv run enrich --dir /home/user/videos --platform instagram

# Re-generate hashtags even if they already exist
uv run enrich --dir /home/user/videos --force
```

### Alternative: virtual environment

If you prefer not to use uv, create a venv manually:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

enrich --dir /home/user/videos
```

You'll need to activate the venv (`source .venv/bin/activate`) each time you open a new terminal.

---

## Platforms

Each major short-form video platform enforces its own hashtag limits — exceed
them and the platform either ignores all your hashtags or demotes the post:

| `--platform` value | Hard limit | What happens if you exceed it |
|---|---|---|
| `youtube` (default) | 15 | YouTube **silently ignores every hashtag** on the video |
| `tiktok` | 5 | TikTok rejects or demotes the post |
| `instagram` | 5 | Instagram rejects or demotes the post |

By default, `min_tags`/`max_tags` in `config.yaml` are set to **3–5**, which is
within the safe range for all three platforms regardless of which one you target.
If you set `max_tags` higher than your chosen platform's hard limit,
`hashtag-enricher` will refuse to start and tell you exactly what to lower it to —
this is intentional, so you never end up shipping a video whose hashtags get
silently dropped.

Select the platform either in `config.yaml` (`platform: tiktok`) or per-run with
`--platform tiktok`, which overrides the config file for that invocation only.

---

## Language detection

`hashtag-enricher` figures out the language for each video in one of two ways:

- **You already know it** — pass `--lang Spanish` (or a short code like `--lang es`),
  or have a `video_language` field in an existing `script.json`. In this case the
  language detector is **never called** — the tool makes a single API call straight
  to hashtag generation in that language.
- **You don't know it / it varies per file** — leave `--lang` unset. The tool makes
  a single combined API call that detects the language *and* generates the hashtags
  at the same time, rather than two separate calls. This keeps cost and latency the
  same as the explicit-language path above, just with the language inferred instead
  of supplied.

In both cases you get exactly **one** LLM call per video — `--lang` is purely about
*whether* that call needs to figure out the language itself, not about making an
extra call.

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
  }
}
```

`source` is `"filename"` when the topic came from the mp4 filename,
or `"script_json"` when it came from a `video_subject` field in an existing `.json` file.

`platform` reflects whichever platform was active for that run (`config.yaml`'s
`platform` setting, or `--platform` if passed).

If a `.json` already existed, the `hashtags` key is **merged in** — all other fields are preserved unchanged.

Progress and results are also written to the log (`logs/enricher.log`) and echoed
to the terminal as each file finishes, e.g.:

```
[2026-06-30 14:02:11] [INFO] ok: my_clip.mp4 → #shorts #romanempire #historyfacts #ancientrome (4 tags, lang=English [detected], platform=youtube, source=filename)
```

---

## MoneyPrinterTurbo integration

If you use [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo),
a `script.json` is automatically placed next to each generated video.

`hashtag-enricher` detects this file and uses the `params.video_subject` field
as the topic — giving the LLM better context than just the filename.
This requires no configuration; it happens automatically.

---

## youtubeuploader integration

If you use [youtubeuploader](https://github.com/porjo/youtubeuploader) to publish your videos,
you can feed the generated hashtags directly into its `-metaJSON` file.

Read the hashtags from `{video_name}.json` and build the meta file before uploading:

```bash
name="my_video"
json_file="${name}.json"
meta_file="${name}_meta.json"

if jq -e '.hashtags' "$json_file" > /dev/null 2>&1; then
    tags_json=$(jq -c '[.hashtags.tags_list[]? | ltrimstr("#")] | unique | .[0:15]' "$json_file")
    description=$(jq -r '.hashtags.tags_string // ""' "$json_file")
else
    tags_json='["shorts"]'
    description=""
fi

cat > "$meta_file" <<EOF
{
    "title": "$(echo "$name" | tr '_' ' ')",
    "description": "$description",
    "tags": $tags_json,
    "privacyStatus": "private",
    "categoryId": "22"
}
EOF

youtubeuploader -filename "${name}.mp4" -metaJSON "$meta_file" ...
```

Hashtags go into both `description` (YouTube displays the first 3 above the title) and `tags`.

---

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit as needed:

```yaml
platform: youtube          # youtube | tiktok | instagram

min_tags: 3
max_tags: 5

max_tag_length: 20         # tags longer than this (after the #) are dropped

banned_tags: []            # empty by default — add your own, e.g. ["#viral"]

always_include:
  - "#shorts"
```

- **`platform`** — which platform's limits to enforce (`youtube`, `tiktok`, or
  `instagram`). Can be overridden per-run with `--platform`. See [Platforms](#platforms).
- **`min_tags` / `max_tags`** — how many hashtags the LLM should generate, not
  counting `always_include`. Must stay within the chosen platform's hard limit, or
  the tool will refuse to start.
- **`max_tag_length`** — hashtags longer than this (counting only the part after
  `#`) are filtered out after generation.
- **`banned_tags`** — hashtags that are always excluded from the output, no matter
  what the LLM generates. **Empty by default** — this is a blank list for you to
  fill in with whatever you personally don't want; the tool doesn't impose an
  opinion here.
- **`always_include`** — tags always prepended to every result, regardless of
  language, in the order listed.

You can also edit the `prompt_detect_language` and `prompt_generate` templates in
`config.yaml` directly if you want to change the generation strategy itself.

---

## Updating

```bash
cd hashtag-enricher && git pull
```

---

## Third-party notices

This project mentions [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)
and [youtubeuploader](https://github.com/porjo/youtubeuploader) for integration purposes only.
These references are purely descriptive. **This project is not affiliated with, sponsored by,
or endorsed by either project, and neither project constitutes an endorsement of hashtag-enricher.**
Use of third-party tools is at your own risk — please review their respective licenses,
terms, and documentation independently.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
