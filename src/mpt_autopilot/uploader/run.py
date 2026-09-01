"""
uploader/run.py — uploads a folder of .mp4 files to YouTube for one account
(`mpt upload`).

Usage:
    mpt upload --account en
    mpt upload --account en --dry-run
    mpt upload --account en --limit 5
    mpt upload --all-accounts
    mpt upload --all-accounts --dry-run

Setup:
    cp .env.example .env
    cp config.example.yaml config.yaml
    cp accounts.example.yaml accounts.yaml
    # edit accounts.yaml with real paths, then get an OAuth token once via
    # youtubeuploader itself (see docs/uploader.md)

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
import shutil
import sys
import time
from pathlib import Path

from mpt_autopilot import notify
from mpt_autopilot.config import ConfigError
from mpt_autopilot.uploader.ledger import (
    is_done,
    mark_moved,
    mark_started,
    mark_uploaded,
    open_ledger,
    sha256_file,
)
from mpt_autopilot.uploader.metadata import load_meta, sidecar_path, to_meta_json
from mpt_autopilot.uploader.settings import (
    Account,
    Settings,
    get_account,
    load_settings,
    validate_account_ready,
)
from mpt_autopilot.uploader.uploader import UploadFailed, UploadLimitExceeded, upload_video

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_QUOTA_STOP = 2


def find_videos(account: Account) -> list[Path]:
    if not account.videos_dir.exists():
        return []
    return sorted(
        f
        for f in account.videos_dir.iterdir()
        if f.is_file() and f.suffix.lower() == ".mp4" and f.stat().st_size > 0
    )


def move_to_uploaded(video: Path, sidecar: Path, uploaded_dir: Path) -> None:
    uploaded_dir.mkdir(parents=True, exist_ok=True)
    dst_video = uploaded_dir / video.name
    dst_video.unlink(missing_ok=True)
    shutil.move(str(video), str(dst_video))
    if sidecar.exists():
        dst_sidecar = uploaded_dir / sidecar.name
        dst_sidecar.unlink(missing_ok=True)
        shutil.move(str(sidecar), str(dst_sidecar))


def run(settings: Settings, account: Account, *, dry_run: bool, limit: int | None) -> int:
    videos = find_videos(account)

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

    conn = open_ledger(settings.ledger_path)
    try:
        for row in conn.execute(
            "SELECT video_name FROM uploads WHERE account = ? AND status = 'uploaded'",
            (account.name,),
        ):
            uploaded_name = row[0]
            video_path = account.videos_dir / uploaded_name
            if video_path.exists():
                content_hash = sha256_file(video_path)
                print(f"[{account.name}] recovering: {uploaded_name} (was uploaded, moving)")
                move_to_uploaded(video_path, sidecar_path(video_path), uploaded_dir)
                mark_moved(conn, account.name, content_hash)

        uploaded = 0
        failed = 0
        stopped_early = False
        self_limited = False
        last_index = len(videos) - 1

        for index, video in enumerate(videos):
            if not video.exists():
                # The crash-recovery loop above moves anything marked 'uploaded'
                # that is still sitting at the source, which invalidates the
                # snapshot find_videos() took before it ran. Without this guard
                # sha256_file() below would raise FileNotFoundError.
                continue
            content_hash = sha256_file(video)
            if is_done(conn, account.name, content_hash):
                print(f"[{account.name}] skip: {video.name} (already uploaded)")
                continue

            meta = load_meta(video, settings.defaults, account_name=account.name)

            if dry_run:
                print(f"[{account.name}] would upload: {video.name}")
                print(f"    title: {meta.title}")
                print(f"    tags:  {', '.join(meta.tags)}")
                print(f"    privacy: {meta.privacy_status}  category: {meta.category_id}")
                print(f"    AI use (containsSyntheticMedia): {meta.contains_synthetic_media}")
                continue

            meta_dir.mkdir(parents=True, exist_ok=True)
            meta_file = meta_dir / f"{video.stem}.json"
            meta_file.write_text(to_meta_json(meta), encoding="utf-8")

            print(f"[{account.name}] uploading: {video.name} ({meta.title})")
            mark_started(conn, account.name, content_hash, video.name)
            try:
                upload_video(
                    settings.uploader_binary,
                    video,
                    meta_file,
                    account.client_secrets,
                    account.token_file,
                )
            except UploadLimitExceeded:
                msg = f"[{account.name}] daily upload limit reached after {uploaded} videos"
                print(msg)
                notify.alert(f"\U0001f534 [upload/{account.name}] {msg}", settings)
                stopped_early = True
                break
            except UploadFailed as exc:
                failed += 1
                print(f"[{account.name}] FAILED: {video.name}: {exc}")
                notify.alert(f"⚠️ [{account.name}] failed: {video.name}", settings)
                if index < last_index:
                    time.sleep(settings.sleep_between_uploads)
                continue

            mark_uploaded(conn, account.name, content_hash)
            move_to_uploaded(video, sidecar_path(video), uploaded_dir)
            mark_moved(conn, account.name, content_hash)
            uploaded += 1
            print(f"[{account.name}] done: {video.name} -> {settings.uploaded_dir_name}/")
            if account.daily_upload_limit is not None and uploaded >= account.daily_upload_limit:
                self_limited = True
                break
            if limit is not None and uploaded >= limit:
                break
            if index < last_index:
                time.sleep(settings.sleep_between_uploads)

    finally:
        conn.close()

    if not dry_run:
        ok = failed == 0 and not stopped_early
        icon = "✅" if ok else "⚠️"
        suffix = (
            " (stopped early: daily quota)"
            if stopped_early
            else " (stopped early: daily_upload_limit reached)"
            if self_limited
            else ""
        )
        notify.alert(
            f"{icon} [upload/{account.name}] uploaded: {uploaded}  failed: {failed}{suffix}",
            settings,
        )
        print(f"[{account.name}] summary: uploaded={uploaded} failed={failed}{suffix}")
    return EXIT_QUOTA_STOP if stopped_early else EXIT_FAILURES if failed else EXIT_OK


def run_all(settings: Settings, *, dry_run: bool, limit: int | None) -> int:
    """Run upload for all configured accounts, returning aggregated exit code."""
    per_account: dict[str, int] = {}
    any_failure = False
    any_quota = False
    for name in sorted(settings.accounts):
        account = settings.accounts[name]
        try:
            code = run(settings, account, dry_run=dry_run, limit=limit)
        except Exception as exc:
            print(f"[{name}] crashed: {exc}", file=sys.stderr)
            code = EXIT_FAILURES
        per_account[name] = code
        if code == EXIT_QUOTA_STOP:
            any_quota = True
        elif code == EXIT_FAILURES:
            any_failure = True

    print("[all-accounts] summary:")
    for name, code in per_account.items():
        label = {EXIT_OK: "ok", EXIT_FAILURES: "failures", EXIT_QUOTA_STOP: "quota-stop"}.get(
            code, str(code)
        )
        print(f"  {name}: {label}")

    if any_failure:
        return EXIT_FAILURES
    if any_quota:
        return EXIT_QUOTA_STOP
    return EXIT_OK


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register the uploader stage's flags on `parser`.

    Kept separate from build_parser() so `mpt`'s subparser and a standalone
    parser share one definition — the CLI owns `--config` at the root level, so
    it is deliberately not registered here.
    """
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--account", help="account name, as defined in accounts.yaml")
    group.add_argument(
        "--all-accounts",
        action="store_true",
        help="process every account in accounts.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be uploaded without calling the YouTube API",
    )
    parser.add_argument("--limit", type=int, default=None, help="only process the first N videos")
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=None,
        help="path to accounts.yaml (default: next to config.yaml)",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpt upload",
        description="Upload a folder of .mp4 files to YouTube.",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"), help="path to config.yaml"
    )
    return add_arguments(parser)


def execute(args: argparse.Namespace, config_path: Path | None = None) -> int:
    """
    Run the uploader stage from already-parsed arguments.

    Returns an exit code instead of calling sys.exit so `mpt run` can aggregate
    stages in one process. The 0/1/2 contract is unchanged:
    EXIT_OK / EXIT_FAILURES / EXIT_QUOTA_STOP.
    """
    if config_path is None:
        config_path = getattr(args, "config", None)

    try:
        settings = load_settings(config_path=config_path, accounts_path=args.accounts_file)
    except (FileNotFoundError, ValueError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURES

    if args.all_accounts:
        return run_all(settings, dry_run=args.dry_run, limit=args.limit)

    try:
        account = get_account(settings, args.account)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURES
    return run(settings, account, dry_run=args.dry_run, limit=args.limit)


def main() -> None:
    sys.exit(execute(build_parser().parse_args()))


if __name__ == "__main__":
    main()
