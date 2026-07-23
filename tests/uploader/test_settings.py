import tempfile
from pathlib import Path

import pytest

from yt_uploader.engine.settings import (
    Account,
    Defaults,
    Settings,
    find_uploader_binary,
    load_settings,
    validate_account_ready,
)


def _settings(uploader_binary: Path) -> Settings:
    return Settings(
        uploader_binary=uploader_binary,
        meta_dir=Path("./meta"),
        sleep_between_uploads=0,
        uploaded_dir_name="old_videos",
        defaults=Defaults(),
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="yt-shorts-uploader",
        ledger_path=Path("./ledger.sqlite"),
        accounts={},
    )


def test_find_uploader_binary_absolute_path_exists(tmp_path):
    binary = tmp_path / "youtubeuploader"
    binary.write_text("#!/bin/bash\n")
    binary.chmod(0o755)
    assert find_uploader_binary(binary) == binary


def test_find_uploader_binary_absolute_path_missing(tmp_path):
    assert find_uploader_binary(tmp_path / "nope") is None


def test_find_uploader_binary_resolves_via_path(monkeypatch, tmp_path):
    # Skip on Windows - shutil.which requires .exe extension
    import sys

    if sys.platform == "win32":
        pytest.skip("Windows requires .exe extension for shutil.which")
    fake = tmp_path / "toolname"
    fake.write_text("#!/bin/bash\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = find_uploader_binary(Path("toolname"))
    assert resolved is not None
    assert resolved.name == "toolname"


def test_find_uploader_binary_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir, nothing to find
    assert find_uploader_binary(Path("definitely-not-a-real-tool")) is None


def test_validate_account_ready_ok(tmp_path):
    binary = tmp_path / "youtubeuploader"
    binary.write_text("#!/bin/bash\n")
    binary.chmod(0o755)
    secrets = tmp_path / "secrets.json"
    secrets.write_text("{}")

    settings = _settings(binary)
    account = Account(
        name="en",
        videos_dir=tmp_path,
        client_secrets=secrets,
        token_file=tmp_path / "token",  # deliberately absent - must not matter
    )
    assert validate_account_ready(settings, account) is None


def test_validate_account_ready_missing_binary(tmp_path):
    settings = _settings(tmp_path / "does-not-exist")
    account = Account(
        name="en",
        videos_dir=tmp_path,
        client_secrets=tmp_path / "secrets.json",
        token_file=tmp_path / "token",
    )
    problem = validate_account_ready(settings, account)
    assert problem is not None
    assert "uploader_binary" in problem


def test_validate_account_ready_missing_client_secrets(tmp_path):
    binary = tmp_path / "youtubeuploader"
    binary.write_text("#!/bin/bash\n")
    binary.chmod(0o755)

    settings = _settings(binary)
    account = Account(
        name="en",
        videos_dir=tmp_path,
        client_secrets=tmp_path / "missing_secrets.json",
        token_file=tmp_path / "token",
    )
    problem = validate_account_ready(settings, account)
    assert problem is not None
    assert "client_secrets" in problem


def test_daily_upload_limit_must_be_positive(tmp_path):
    with (
        tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as accounts_file,
        tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as config_file,
    ):
        accounts_file.write(
            "accounts:\n  en:\n"
            "    videos_dir: /tmp/v\n"
            "    client_secrets: /tmp/s.json\n"
            "    token_file: /tmp/t\n"
            "    daily_upload_limit: 0\n"
        )
        config_file.write("")

    with pytest.raises(ValueError, match="daily_upload_limit must be positive"):
        load_settings(
            config_path=Path(config_file.name),
            accounts_path=Path(accounts_file.name),
        )
