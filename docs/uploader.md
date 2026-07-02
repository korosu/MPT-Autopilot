# yt-shorts-uploader

![lint](https://github.com/korosu/yt-shorts-uploader/actions/workflows/lint.yml/badge.svg)

Uploads a folder of `.mp4` files to YouTube, one account (channel) at a time.
Doesn't care where the videos came from - point it at any folder.

Built to sit downstream of [mpt-batch](https://github.com/korosu/mpt-batch), but
has no dependency on it: any tool that drops MP4s (optionally with a metadata
sidecar) into a folder works.

## What it does

- Scans an account's `videos_dir` for `*.mp4`
- For each video, looks for a sidecar `<name>.json` with title/description/tags
  - No sidecar? Title is derived from the filename.
- Uploads via [youtubeuploader](https://github.com/porjo/youtubeuploader)
- Moves successfully uploaded videos into `old_videos/` (configurable name)
- Stops early and alerts if YouTube's daily upload quota is hit
- Optional Telegram notification on completion / failure / quota hit

## What it doesn't do

- No OAuth flow of its own - use `youtubeuploader` directly to mint the first
  token per account (see **Authentication** below)
- No syncing videos between machines - that's a separate concern, keep it in
  whatever glue script deploys your pipeline
- No video generation - see [mpt-batch](https://github.com/korosu/mpt-batch)
  if you need that half

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or plain `pip`
- The `youtubeuploader` binary, built and reachable on the machine
- One Google Cloud OAuth client per YouTube account you want to upload to

## Install

```bash
git clone https://github.com/korosu/yt-shorts-uploader.git
cd yt-shorts-uploader
uv sync
```

Or with plain pip:

```bash
pip install -e .
```

## Setup

```bash
cp .env.example .env
cp config.yaml.example config.yaml
cp accounts.example.yaml accounts.yaml
```

Edit `accounts.yaml` with the real paths for each account:

```yaml
accounts:
  en:
    videos_dir: "/your/path/to/videos/en"
    client_secrets: "/root/youtubeuploader/client_secrets_en.json"
    token_file: "/root/youtubeuploader/request.token.en"
```

`config.yaml` has sane defaults (private uploads, category 22, 5s between
uploads) - only edit it if you want to change those.

## Authentication

`youtubeuploader` handles OAuth itself; this tool just shells out to it. Get
the first token per account **once**, by hand:

```bash
youtubeuploader \
  -secrets /root/youtubeuploader/client_secrets_en.json \
  -cache   /root/youtubeuploader/request.token.en \
  -filename /path/to/any/test.mp4
```

It prints a URL, you authorize in a browser, and it caches a refresh token at
`-cache`. After that, `upload --account en` reuses it silently.

## Usage

```bash
uv run upload --account en
```

Preview without uploading anything:

```bash
uv run upload --account en --dry-run
```

Only process the first few (useful for testing a new account):

```bash
uv run upload --account en --limit 3
```

## Metadata sidecar

Drop a `<name>.json` next to `<name>.mp4` to control title/description/tags.
It uses the exact schema `youtubeuploader` itself expects for `-metaJSON`, so
there's no translation step:

```json
{
  "title": "What Air Traffic Controllers Never Tell Passengers",
  "description": "#shorts #aviation #facts",
  "tags": ["shorts", "aviation", "facts"],
  "privacyStatus": "private",
  "categoryId": "22"
}
```

Every field is optional - anything missing falls back to `defaults:` in
`config.yaml`. No sidecar at all? The title is the filename with `_`/`-`
replaced by spaces, e.g. `air_traffic_controllers.mp4` → "air traffic
controllers".

## Multiple accounts

Each key under `accounts:` in `accounts.yaml` is independent - its own videos
folder, its own OAuth credentials, its own `old_videos/`. Add as many as you
have channels; there's no built-in concept of "language", just accounts. Run
them from cron, one line per account:

```cron
30 19 * * * cd /root/yt-shorts-uploader && uv run upload --account en
35 19 * * * cd /root/yt-shorts-uploader && uv run upload --account es
```

## Exit codes

`upload` returns a specific code so cron/orchestration can tell these apart:

| Code | Meaning |
|------|---------|
| `0`  | Nothing to do, or every video uploaded successfully |
| `1`  | Config/account problem, or at least one video failed to upload |
| `2`  | Stopped early - YouTube's daily upload quota was hit (videos already uploaded in that run are still moved to `old_videos/`) |

## Quota handling

YouTube's Data API gives every project a fixed daily upload quota. When
`youtubeuploader` reports `uploadLimitExceeded`, this tool stops that
account's run immediately (rather than retrying and burning more of the
quota on failed attempts) and sends a Telegram alert if configured. Already
uploaded videos in that run stay moved to `old_videos/`; the rest are left in
place for tomorrow's run.

## Development

```bash
uv sync --extra dev   # or: pip install -e ".[dev]"
pytest tests/ -v
ruff check .
ruff format --check .
```

Before doing any real work, `run()` checks that `uploader_binary` resolves
(absolute path or found on `PATH`) and that the account's `client_secrets`
file exists - a bad path in `config.yaml`/`accounts.yaml` fails fast with a
clear message instead of a stack trace partway through a batch.
