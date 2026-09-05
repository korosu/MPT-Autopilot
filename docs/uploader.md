# `mpt upload` — the uploader stage

Uploads a folder of `.mp4` files to YouTube, one account (channel) at a time.
Doesn't care where the videos came from - point it at any folder.

> **This is a wrapper around [porjo/youtubeuploader](https://github.com/porjo/youtubeuploader).**
> The tool does not implement the YouTube API itself — it orchestrates calls to
> youtubeuploader, adding multi-account support, metadata handling, retry logic,
> and optional notifications.

> This stage was the standalone `yt-shorts-uploader` tool before the merge. Its
> behaviour — including its load-bearing exit codes — is unchanged; it is now
> reached through `mpt upload` and configured from the `uploader:` section of the
> one shared `config.yaml`, plus the still-separate `accounts.yaml`.

Built to sit downstream of `mpt batch` and `mpt enrich`, but has no dependency on
either: any tool that drops MP4s (optionally with a metadata sidecar) into a
folder works.

## Where it sits in the pipeline

```
mpt refill  →  mpt batch  →  mpt enrich  →  mpt upload
```

`mpt batch` renders pending jobs into the exports directory, `mpt enrich` writes
hashtags into the sidecar `.json` next to each video, and `mpt upload` reads that
sidecar and uploads. `mpt run` does all four per language and maps each language
to an uploader account — by default the account named after the language code,
with `pipeline.accounts` overriding that.

## What it does

- Scans an account's `videos_dir` for `*.mp4`
- For each video, looks for a sidecar `<name>.json` with title/description/tags
  - No sidecar? Title is derived from the filename.
- Uploads via [youtubeuploader](https://github.com/porjo/youtubeuploader)
- Moves successfully uploaded videos into `old_videos/` (configurable name)
- Stops early and alerts if YouTube's daily upload quota is hit
- Optional Telegram notifications on completion / failure / quota hit

## What it doesn't do

- No OAuth flow of its own - use `youtubeuploader` directly to mint the first
  token per account (see **Authentication** below)
- No syncing videos between machines - that's a separate concern, keep it in
  whatever glue script deploys your pipeline
- No video generation - that's `mpt batch`, see [batch.md](batch.md)

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or plain `pip`
- The `youtubeuploader` binary, built and reachable on the machine
- One Google Cloud OAuth client per YouTube account you want to upload to

## Install

The whole suite installs once, from the repository root:

```bash
git clone https://github.com/korosu/mpt-autopilot.git
cd mpt-autopilot
uv sync
```

Or with plain pip:

```bash
pip install -e .
```

Either way you get one `mpt` command; `uv run mpt ...` works without activating
anything.

## Setup

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp accounts.example.yaml accounts.yaml
```

`accounts.yaml` stays a **separate file** on purpose: it points at OAuth secrets,
so it is the one file you are most likely to keep outside the repository. Edit it
with the real paths for each account:

```yaml
accounts:
  en:
    videos_dir: "/your/path/to/videos/en"
    client_secrets: "/root/youtubeuploader/client_secrets_en.json"
    token_file: "/root/youtubeuploader/request.token.en"
    # daily_upload_limit: 10
```

`mpt upload` looks for `accounts.yaml` **next to `config.yaml`**. Point somewhere
else with `uploader.accounts_file` in `config.yaml`, or per-run with
`--accounts-file`.

`videos_dir`, `client_secrets`, and `token_file` are required per account;
`daily_upload_limit` is optional and stops that account after N successful uploads
in one run (YouTube's own quota is a separate thing, detected independently — see
[Exit codes](#exit-codes)). Unlike `config.yaml` paths, these are taken as given
(with `~` expanded), so use absolute paths.

The `uploader:` section of `config.yaml` has sane defaults (private uploads,
category 22, 5s between uploads) - only edit it if you want to change those. See
[Configuration](#configuration).

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
`-cache`. After that, `mpt upload --account en` reuses it silently.

## Usage

Exactly one of `--account` or `--all-accounts` is required.

Upload for a single account:

```bash
mpt upload --account en
```

Process all accounts in one run:

```bash
mpt upload --all-accounts
```

Preview without uploading anything:

```bash
mpt upload --account en --dry-run
mpt upload --all-accounts --dry-run
```

Only process the first few (useful for testing a new account):

```bash
mpt upload --account en --limit 3
```

Read a different config, or a different accounts file:

```bash
mpt --config /srv/mpt/config.yaml upload --account en
mpt upload --config /srv/mpt/config.yaml --account en     # equivalent
mpt upload --account en --accounts-file /secure/accounts.yaml
```

### Flags

| Flag | Effect |
|---|---|
| `--account NAME` | Upload for one account, as named in `accounts.yaml` |
| `--all-accounts` | Process every account in `accounts.yaml`, in name order |
| `--dry-run` | Print what would be uploaded, call nothing |
| `--limit N` | Stop after N successful uploads per account |
| `--accounts-file PATH` | Override the `accounts.yaml` location for this run |
| `--config PATH` | Which `config.yaml` to read (default `./config.yaml`) |

`--config` works both before and after the subcommand, so muscle memory from the
old separate command still works.

A `--dry-run` prints the resolved metadata per video without touching the YouTube
API, and deliberately skips the binary/credentials validation — you can preview a
queue on a machine that has no `youtubeuploader` installed:

```
[en] would upload: air_traffic_controllers.mp4
    title: air traffic controllers
    tags:  shorts, aviation, facts
    privacy: private  category: 22
    AI use (containsSyntheticMedia): True
```

The ledger is still consulted, so a dry run also shows you which videos would be
skipped as already uploaded. It lists every remaining candidate, though —
`--limit` only caps real uploads. Note that a dry run is not entirely read-only:
it opens (and therefore creates) the ledger, and the crash-recovery pass
described under [Crash-safe resumes](#crash-safe-resumes-sqlite-ledger) still
moves anything left marked `uploaded` into `old_videos/`.

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
  "categoryId": "22",
  "containsSyntheticMedia": true
}
```

Every field is optional - anything missing falls back to `uploader.defaults` in
`config.yaml`. No sidecar at all? The title is the filename with `_`/`-`
replaced by spaces, e.g. `air_traffic_controllers.mp4` → "air traffic
controllers". If the filename ends with `_<account name>`, that suffix is
stripped first. Titles are truncated at 100 characters and descriptions at 5000,
YouTube's own limits.

`containsSyntheticMedia` is YouTube's "AI use" disclosure for realistic
altered/synthetic content. It defaults to `true`
(`uploader.defaults.contains_synthetic_media`), because MoneyPrinterTurbo output
is AI-generated; a sidecar can override it per video.

The rendered metadata is written to `<uploader.meta_dir>/<account>/<name>.json`
before each upload and handed to `youtubeuploader` as `-metaJSON`, so you can
inspect exactly what was sent.

### Hashtag placement

The `tags` field in the sidecar is what `mpt enrich` writes (its flat `tags`
key). Use `hashtag_placement` to control where they go in the YouTube upload:

```yaml
uploader:
  defaults:
    hashtag_placement: both  # default
```

Three modes:

| Mode | Behavior |
|------|----------|
| `tags` | Writes to hidden metadata field |
| `description` | Appends as visible hashtags to description |
| `both` | Applies both independently (default) |

Anything else is a config error at startup.

Example sidecar from `mpt enrich`:
```json
{
  "hashtags": {"tags_list": ["#shorts", "#history"]},
  "tags": ["#shorts", "#history"]
}
```

With `hashtag_placement: both`, this becomes:
```json
{
  "title": "My Video",
  "description": "My Video #shorts #history",
  "tags": ["shorts", "history"],
  "privacyStatus": "private",
  "categoryId": "22",
  "containsSyntheticMedia": true
}
```

The two placements respect different YouTube limits, which is why they are
applied independently: the hidden `tags` field is merged on top of
`uploader.defaults.tags` with the leading `#` stripped and trimmed to a 500-character
budget, while description hashtags are appended as-is and capped at 15 total
(counting any hashtags already present in the title and description).

## Multiple accounts

Each key under `accounts:` in `accounts.yaml` is independent - its own videos
folder, its own OAuth credentials, its own `old_videos/`, its own `meta_dir`
subfolder. Add as many as you have channels; the uploader itself has no concept
of "language", just accounts.

`mpt run` is what connects the two ideas: it uploads each language to the account
named after its language code, unless `pipeline.accounts` in `config.yaml` maps it
elsewhere:

```yaml
pipeline:
  accounts:
    en: main-channel
    es: spanish-channel
```

If your accounts are already named `en` / `es`, that block can be deleted
entirely.

Recommended: use `--all-accounts` to process every account in one invocation:

```cron
30 19 * * * /usr/local/bin/mpt --config /srv/mpt/config.yaml upload --all-accounts
```

Or run specific accounts individually:

```cron
30 19 * * * /usr/local/bin/mpt --config /srv/mpt/config.yaml upload --account en
```

Passing `--config` with an absolute path is worth doing even when the working
directory looks right: every relative path in `config.yaml` resolves against the
config file's own location, never the current directory, and the `.env` beside it
is picked up automatically. If you run the whole chain, one `mpt run` entry
replaces these — see [pipeline.md](pipeline.md).

## Exit codes

`mpt upload` returns a specific code so cron/orchestration can tell these apart:

| Code | Meaning |
|------|----------|
| `0`  | Nothing to do, or every video uploaded successfully |
| `1`  | Config/account problem, or at least one video failed to upload |
| `2`  | Stopped early - YouTube's daily upload quota was hit (videos already uploaded in that run are still moved to `old_videos/`) |

With `--all-accounts`: exit 1 if any account had failures, else 2 if any account hit quota, else 0.

`mpt run` aggregates the same three codes across every language and stage, with
failure taking precedence over a quota stop — so a quota stop stays
distinguishable from something actually being broken. See
[pipeline.md](pipeline.md).

Note that an account stopping on its own `daily_upload_limit` is **not** a quota
stop: that is a self-imposed ceiling, everything asked for succeeded, and the exit
code stays 0. Only YouTube's `uploadLimitExceeded` produces 2.

## Crash-safe resumes (SQLite ledger)

A SQLite ledger sits next to `config.yaml` (`yt-uploader-ledger.sqlite`).
Before each upload it records the video's SHA-256 content hash and status
(`started` → `uploaded` → `moved`). If the process crashes after the upload
is recorded as `uploaded` but before the file is moved, the next run skips
that video - no duplicate upload, and the leftover file is moved into
`old_videos/` during a recovery pass at the start of the run.

The ledger's location is deliberately **not** configurable and deliberately not
relative to the working directory: a cron entry that started in the wrong place
would otherwise create a fresh, empty ledger and happily re-upload everything.
It follows `config.yaml`, so `--config /srv/mpt/config.yaml` always uses
`/srv/mpt/yt-uploader-ledger.sqlite`.

Dedup is by content hash, not filename, and is scoped per account — the same
video can legitimately go to two channels.

## Retries

A failed upload is attempted up to 3 times with exponential backoff between
attempts (2s, then 4s), but **only** for failures that provably happen before
youtubeuploader finalizes the upload session: HTTP 503, `backendError`,
deadline/timeout, `temporary`, and `try again later`. Any other non-zero exit may
be post-finalize, so it fails immediately rather than risk a duplicate upload. A
single upload is given 600 seconds before it is treated as timed out.

A failed video does not stop the run: it is counted, alerted, and the account
continues with the next file. The account's exit code becomes 1.

## Configuration

Everything lives under the `uploader:` section of the one shared `config.yaml`:

```yaml
uploader:
  # The youtubeuploader binary. A bare name is looked up on PATH.
  uploader_binary: "/root/youtubeuploader/youtubeuploader"

  # Where per-account metaJSON files are written before each upload.
  meta_dir: "/root/youtubeuploader/meta"

  # accounts.yaml location. Defaults to accounts.yaml beside config.yaml.
  # accounts_file: "./accounts.yaml"

  sleep_between_uploads: 5
  uploaded_dir_name: "old_videos"

  defaults:
    privacy_status: "private"   # private | unlisted | public
    category_id: "22"           # 22 = People & Blogs
    tags: ["shorts"]
    hashtag_placement: both     # tags | description | both
    contains_synthetic_media: true
```

- **`uploader_binary`** — path to the `youtubeuploader` executable. A bare name
  (e.g. `youtubeuploader`) is looked up on `PATH`; anything with a directory
  component is checked directly. Default: `youtubeuploader`. Unlike the other
  paths here it is taken as written (with `~` expanded), so prefer a bare name or
  an absolute path over a relative one.
- **`meta_dir`** — where the generated metaJSON files go, one subdirectory per
  account. Default: `./meta`.
- **`accounts_file`** — overrides the default `accounts.yaml`-beside-`config.yaml`
  lookup. `--accounts-file` overrides both.
- **`sleep_between_uploads`** — seconds to pause between uploads so YouTube does
  not see a burst. Must be ≥ 0. Default: 5. No sleep is added after the last
  video.
- **`uploaded_dir_name`** — the subfolder inside each account's `videos_dir` that
  successfully uploaded videos and their sidecars are moved into. Default:
  `old_videos`.
- **`defaults`** — used when a video has no sidecar, or the sidecar omits a field:
  `privacy_status` (default `private`), `category_id` (default `"22"`), `tags`
  (default `["shorts"]`), `hashtag_placement` (default `both`), and
  `contains_synthetic_media` (default `true`).

`meta_dir` and `accounts_file` resolve against `config.yaml`'s own directory, not
the directory you run `mpt` from. The paths inside `accounts.yaml` are separate:
they are used as written, so keep them absolute.

## Notifications

Optional Telegram alerts on completion / failure / quota hit.

Telegram credentials go in `.env`:
```
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Leave both empty to disable notifications entirely. The prefix on each message
comes from `config.yaml`'s shared `telegram_prefix` (default `mpt-autopilot`); add
`telegram_prefix` inside the `uploader:` section to override it for this stage
only. Messages are additionally tagged with the account, e.g.
`[mpt-autopilot] ✅ [upload/en] uploaded: 7  failed: 0`. The notifier is the
shared `src/mpt_autopilot/notify.py` used by every stage.

## Development

```bash
uv sync --extra dev   # or: pip install -e ".[dev]"
uv run pytest tests/ -v
uv run ruff check .
uv run ruff format --check .
uv run pyright src
```

Before doing any real work, `run()` checks that `uploader_binary` resolves
(absolute path or found on `PATH`) and that the account's `client_secrets`
file exists - a bad path in `config.yaml`/`accounts.yaml` fails fast with a
clear message instead of a stack trace partway through a batch. `token_file` is
deliberately not checked: it does not exist until the first OAuth run, and
`youtubeuploader` itself explains the problem if it is missing when it matters.

The stage's code lives in `src/mpt_autopilot/uploader/`:
`run.py` (flags and orchestration), `settings.py` (`uploader:` + `accounts.yaml`),
`uploader.py` (the youtubeuploader subprocess and retries), `metadata.py`
(sidecar → metaJSON), and `ledger.py` (the SQLite dedup ledger).

## Related

- [pipeline.md](pipeline.md) — `mpt run`, the whole chain
- [enricher.md](enricher.md) — `mpt enrich`, which writes the sidecars this stage reads
- [batch.md](batch.md) — `mpt batch`, which renders the videos
- [migration.md](migration.md) — coming from the standalone `yt-shorts-uploader`,
  including moving an existing ledger next to `config.yaml`
