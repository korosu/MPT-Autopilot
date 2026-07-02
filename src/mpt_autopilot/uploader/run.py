"""
upload - uploads a folder of .mp4 files to YouTube for one account.

Usage:
    uv run upload --account en
    uv run upload --account en --dry-run
    uv run upload --account en --limit 5

Setup:
    cp .env.example .env
    cp config.yaml.example config.yaml
    cp accounts.example.yaml accounts.yaml
    # edit accounts.yaml with real paths, then get an OAuth token once via
    # youtubeuploader itself (see README)

Each video is uploaded together with an optional sidecar <name>.json (same
schema youtubeuploader itself expects for -metaJSON). No sidecar -> title is
derived from the filename. On success, the video and its sidecar are moved
into <account videos_dir>/<uploaded_dir_name>/.

Exit codes:
    0 - nothing to do, or every video uploaded successfully
    1 - config/account problem, or at least one video failed to upload
    2 - stopped early because YouTube's daily upload quota was hit
        (videos uploaded before the stop are still moved to old_videos/)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from yt_uploader.engine import notify
from yt_uploader.engine.metadata import load_meta, sidecar_path, to_meta_json
from yt_uploader.engine.settings import (
    Account,
    Settings,
    get_account,
    load_settings,
    validate_account_ready,
)
from yt_uploader.engine.uploader import UploadFailed, UploadLimitExceeded, upload_video

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_QUOTA_STOP = 2


def find_videos(account: Account) -> list[Path]:
    if not account.videos_dir.exists():
        return []
    return sorted(
        f for f in account.videos_dir.iterdir() if f.is_file() and f.suffix.lower() == ".mp4"
    )


def move_to_uploaded(video: Path, sidecar: Path, uploaded_dir: Path) -> None:
    uploaded_dir.mkdir(parents=True, exist_ok=True)
    dst_video = uploaded_dir / video.name
    dst_video.unlink(missing_ok=True)
    video.rename(dst_video)
    if sidecar.exists():
        dst_sidecar = uploaded_dir / sidecar.name
        dst_sidecar.unlink(missing_ok=True)
        sidecar.rename(dst_sidecar)


def run(settings: Settings, account: Account, *, dry_run: bool, limit: int | None) -> int:
    videos = find_videos(account)
    if limit is not None:
        videos = videos[:limit]

    if not videos:
        if not account.videos_dir.exists():
            print(f"[{account.name}] videos_dir does not exist: {account.videos_dir}")
        else:
            print(f"[{account.name}] no .mp4 files found in {account.videos_dir}")
        return EXIT_OK

    if not dry_run:
        problem = validate_account_ready(settings, account)
        if problem:
            print(f"[{account.name}] error: {problem}", file=sys.stderr)
            return EXIT_FAILURES

    meta_dir = settings.meta_dir / account.name
    uploaded_dir = account.videos_dir / settings.uploaded_dir_name

    uploaded = 0
    failed = 0
    stopped_early = False
    last_index = len(videos) - 1

    for index, video in enumerate(videos):
        meta = load_meta(video, settings.defaults)

        if dry_run:
            print(f"[{account.name}] would upload: {video.name}")
            print(f"    title: {meta.title}")
            print(f"    tags:  {', '.join(meta.tags)}")
            print(f"    privacy: {meta.privacy_status}  category: {meta.category_id}")
            continue

        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_file = meta_dir / f"{video.stem}.json"
        meta_file.write_text(to_meta_json(meta), encoding="utf-8")

        print(f"[{account.name}] uploading: {video.name} ({meta.title})")
        try:
            upload_video(
                settings.uploader_binary,
                video,
                meta_file,
                account.client_secrets,
                account.token_file,
            )
        except UploadLimitExceeded:
            msg = (
                f"[{account.name}] daily upload limit reached after {uploaded} video(s) - stopping"
            )
            print(msg)
            notify.alert(settings, f"\U0001f534 [upload/{account.name}] {msg}")
            stopped_early = True
            break
        except UploadFailed as exc:
            failed += 1
            print(f"[{account.name}] FAILED: {video.name}: {exc}")
            notify.alert(settings, f"\u26a0\ufe0f [upload/{account.name}] failed: {video.name}")
            if index < last_index:
                time.sleep(settings.sleep_between_uploads)
            continue

        move_to_uploaded(video, sidecar_path(video), uploaded_dir)
        uploaded += 1
        print(f"[{account.name}] done: {video.name} -> {settings.uploaded_dir_name}/")
        if index < last_index:
            time.sleep(settings.sleep_between_uploads)

    if not dry_run:
        ok = failed == 0 and not stopped_early
        icon = "\u2705" if ok else "\u26a0\ufe0f"
        suffix = " (stopped early: daily quota)" if stopped_early else ""
        notify.alert(
            settings,
            f"{icon} [upload/{account.name}] uploaded: {uploaded}  failed: {failed}{suffix}",
        )
        print(f"[{account.name}] summary: uploaded={uploaded} failed={failed}{suffix}")

    if stopped_early:
        return EXIT_QUOTA_STOP
    return EXIT_FAILURES if failed else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upload",
        description="Upload a folder of .mp4 files to YouTube for one account.",
    )
    parser.add_argument(
        "--account", required=True, help="account name, as defined in accounts.yaml"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be uploaded without calling the YouTube API",
    )
    parser.add_argument("--limit", type=int, default=None, help="only process the first N videos")
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"), help="path to config.yaml"
    )
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=Path("accounts.yaml"),
        help="path to accounts.yaml",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        settings = load_settings(config_path=args.config, accounts_path=args.accounts_file)
        account = get_account(settings, args.account)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURES)

    exit_code = run(settings, account, dry_run=args.dry_run, limit=args.limit)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
