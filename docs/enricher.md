# hashtag-enricher

Generate relevant YouTube/TikTok hashtags for your video files using an LLM API.

Point it at a folder of `.mp4` files — it figures out the topic from the filename,
calls an LLM, and saves the hashtags into a `.json` file next to each video.
No video generator or special toolchain required.

---

## Features

- **Works with any mp4 file** — topic is read from the filename by default
- **Auto-detects language** — no need to specify it; detected per-file via LLM
- **Optional MoneyPrinterTurbo integration** — if a `script.json` exists next to the video, the richer `video_subject` field is used automatically
- **Provider-agnostic** — works with OpenAI, Groq, Together, local Ollama, GitHub Models — just change `LLM_BASE_URL`
- **Safe by default** — never overwrites existing hashtags unless `--force` is passed
- **Dry-run mode** — preview output without writing anything

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
```

Open `.env` and add your API key:

```
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

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
uv run enrich.py

# Scan a specific folder
uv run enrich.py --dir ./videos

# Process a single file
uv run enrich.py --file ./videos/my_clip.mp4

# Force a specific language for all files (skips LLM language detection → faster)
uv run enrich.py --dir ./videos --lang Spanish
uv run enrich.py --dir ./videos --lang en        # short codes work too

# Preview what would be generated without saving
uv run enrich.py --dir ./videos --dry-run

# Re-generate hashtags even if they already exist
uv run enrich.py --dir ./videos --force
```

### Alternative: virtual environment

If you prefer not to use uv, create a venv manually:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python enrich.py --dir ./videos
```

You'll need to activate the venv (`source .venv/bin/activate`) each time you open a new terminal.

---

## Output

For each `*.mp4` file, a `{video_name}.json` is created (or updated) next to it:

```json
{
  "hashtags": {
    "tags_list": ["#shorts", "#romanempire", "#historyfacts", "#ancientrome"],
    "tags_string": "#shorts #romanempire #historyfacts #ancientrome",
    "generated_at": "2025-06-17T14:00:00Z",
    "model": "gpt-4o-mini",
    "detected_language": "English",
    "source": "filename"
  }
}
```

`source` is `"filename"` when the topic came from the mp4 filename,
or `"script_json"` when it came from a `video_subject` field in an existing `.json` file.

If a `.json` already existed, the `hashtags` key is **merged in** — all other fields are preserved unchanged.

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

Edit `config.yaml` to change tag limits, always-included tags, or prompt wording:

```yaml
max_tags: 15

always_include:
  - "#shorts"
```

`always_include` tags are always prepended to every result, regardless of language.

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
