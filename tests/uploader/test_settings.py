import tempfile
from pathlib import Path

import pytest

from mpt_autopilot.uploader.settings import (
    Account,
    Defaults,
    Settings,
    find_uploader_binary,
    load_settings,
    validate_account_ready,
)
from tests.uploader.helpers import make_uploader_binary


def _settings(uploader_binary: Path) -> Settings:
    return Settings(
        uploader_binary=uploader_binary,
        meta_dir=Path("./meta"),
        sleep_between_uploads=0,
        uploaded_dir_name="old_videos",
        defaults=Defaults(),
        telegram_token="",
        telegram_chat_id="",
        telegram_prefix="mpt-autopilot",
        ledger_path=Path("./ledger.sqlite"),
        accounts={},
    )


def test_find_uploader_binary_absolute_path_exists(tmp_path):
    binary = make_uploader_binary(tmp_path)
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
    binary = make_uploader_binary(tmp_path)
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
    settings = _settings(make_uploader_binary(tmp_path))
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


def test_contains_synthetic_media_defaults_true_when_unset(tmp_path):
    accounts_path = tmp_path / "accounts.yaml"
    config_path = tmp_path / "config.yaml"
    accounts_path.write_text("accounts: {}\n")
    config_path.write_text("")

    settings = load_settings(config_path=config_path, accounts_path=accounts_path)
    assert settings.defaults.contains_synthetic_media is True


def test_contains_synthetic_media_reads_false_from_config(tmp_path):
    accounts_path = tmp_path / "accounts.yaml"
    config_path = tmp_path / "config.yaml"
    accounts_path.write_text("accounts: {}\n")
    # Now sectioned: `defaults:` lives under `uploader:`, not at the top level.
    config_path.write_text("uploader:\n  defaults:\n    contains_synthetic_media: false\n")

    settings = load_settings(config_path=config_path, accounts_path=accounts_path)
    assert settings.defaults.contains_synthetic_media is False


def test_load_settings_reads_accounts_from_uploader_section(tmp_path):
    """accounts_file under `uploader:` is resolved relative to config.yaml."""
    (tmp_path / "channels.yaml").write_text(
        "accounts:\n  en:\n    videos_dir: ./v\n    client_secrets: ./s.json\n    token_file: ./t\n"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("uploader:\n  accounts_file: ./channels.yaml\n")

    settings = load_settings(config_path=config_path)
    assert set(settings.accounts) == {"en"}


def test_ledger_lives_next_to_config(tmp_path):
    """The ledger must not follow cwd, or a cron run would start a fresh one."""
    (tmp_path / "accounts.yaml").write_text("accounts: {}\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("")

    settings = load_settings(config_path=config_path)
    assert settings.ledger_path.parent == tmp_path
