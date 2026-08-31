from __future__ import annotations

from pathlib import Path

import pytest

from yt_uploader.engine.settings import Account, Defaults, Settings
from yt_uploader.upload import (
    EXIT_FAILURES,
    EXIT_OK,
    EXIT_QUOTA_STOP,
    build_parser,
    run_all,
)


def _settings(tmp_path: Path) -> Settings:
    binary = tmp_path / "youtubeuploader"
    binary.write_text("#!/bin/bash\n")
    binary.chmod(0o755)
    return Settings(
        uploader_binary=binary,
        meta_dir=tmp_path / "meta",
        sleep_between_uploads=0,
        uploaded_dir_name="old_videos",
        defaults=Defaults(),
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="yt-shorts-uploader",
        ledger_path=tmp_path / "ledger.sqlite",
        accounts={},
    )


def _account(tmp_path: Path, name: str, n_videos: int) -> Account:
    videos_dir = tmp_path / f"videos_{name}"
    videos_dir.mkdir()
    for i in range(n_videos):
        # Distinct content per video to avoid SHA-256 hash collisions in ledger dedup
        (videos_dir / f"video_{i}.mp4").write_bytes(f"video_{i} content".encode())
    secrets = tmp_path / f"secrets_{name}.json"
    secrets.write_text("{}")
    return Account(
        name=name,
        videos_dir=videos_dir,
        client_secrets=secrets,
        token_file=tmp_path / f"token_{name}",
    )


def test_all_accounts_runs_each(tmp_path, monkeypatch):
    accounts = {
        "en": _account(tmp_path, "en", 2),
        "es": _account(tmp_path, "es", 1),
    }
    settings = _settings(tmp_path)
    settings.accounts = accounts

    called_accounts: list[str] = []

    def fake_run(settings_arg, account, *, dry_run, limit):
        called_accounts.append(account.name)
        return EXIT_OK

    monkeypatch.setattr("yt_uploader.upload.run", fake_run)

    exit_code = run_all(settings, dry_run=False, limit=None)

    assert exit_code == EXIT_OK
    assert set(called_accounts) == {"en", "es"}


def test_all_accounts_one_crashes_others_continue(tmp_path, monkeypatch):
    accounts = {
        "en": _account(tmp_path, "en", 1),
        "es": _account(tmp_path, "es", 1),
    }
    settings = _settings(tmp_path)
    settings.accounts = accounts

    def fake_run(settings_arg, account, *, dry_run, limit):
        if account.name == "en":
            raise RuntimeError("simulated crash")
        return EXIT_OK

    monkeypatch.setattr("yt_uploader.upload.run", fake_run)

    exit_code = run_all(settings, dry_run=False, limit=None)

    assert exit_code == EXIT_FAILURES  # any failure dominates


def test_all_accounts_mixed_quota_and_failure(tmp_path, monkeypatch):
    accounts = {
        "en": _account(tmp_path, "en", 1),
        "es": _account(tmp_path, "es", 1),
    }
    settings = _settings(tmp_path)
    settings.accounts = accounts

    def fake_run(settings_arg, account, *, dry_run, limit):
        if account.name == "en":
            return EXIT_QUOTA_STOP
        return EXIT_FAILURES

    monkeypatch.setattr("yt_uploader.upload.run", fake_run)

    exit_code = run_all(settings, dry_run=False, limit=None)

    assert exit_code == EXIT_FAILURES  # failure wins over quota


def test_account_and_all_accounts_mutually_exclusive():
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--account", "en", "--all-accounts"])
    assert exc_info.value.code == 2


def test_all_accounts_dry_run(tmp_path, monkeypatch):
    accounts = {"en": _account(tmp_path, "en", 1)}
    settings = _settings(tmp_path)
    settings.accounts = accounts

    called = []
    monkeypatch.setattr("yt_uploader.upload.run", lambda *a, **k: called.append(1) or EXIT_OK)

    exit_code = run_all(settings, dry_run=True, limit=None)

    assert exit_code == EXIT_OK
    assert called == [1]


def test_run_does_not_notify_in_dry_run(tmp_path, monkeypatch):
    from yt_uploader.upload import run

    accounts = {"en": _account(tmp_path, "en", 1)}
    settings = _settings(tmp_path)
    settings.accounts = accounts

    notify_calls: list[str] = []
    monkeypatch.setattr(
        "yt_uploader.engine.notify.requests.post", lambda *a, **kw: notify_calls.append("called")
    )

    run(settings, list(accounts.values())[0], dry_run=True, limit=None)

    assert notify_calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
