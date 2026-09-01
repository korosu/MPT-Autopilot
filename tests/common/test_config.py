"""
Tests for the shared config loader: sectioning, path resolution relative to
config.yaml, the shared langs block, and the telegram prefix fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mpt_autopilot import config as shared_config
from mpt_autopilot.config import ConfigError


@pytest.fixture(autouse=True)
def _no_config_cache():
    """Each test gets a clean loader cache — configs are cached per path."""
    shared_config.clear_cache()
    yield
    shared_config.clear_cache()


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_config_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        shared_config.load(tmp_path / "nope.yaml")


def test_empty_config_is_valid(tmp_path):
    cfg = shared_config.load(_write(tmp_path, ""))
    assert cfg.raw == {}
    assert cfg.section("batch") == {}


def test_top_level_must_be_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="top level"):
        shared_config.load(_write(tmp_path, "- just\n- a\n- list\n"))


def test_unknown_section_name_is_a_programming_error(tmp_path):
    cfg = shared_config.load(_write(tmp_path, ""))
    with pytest.raises(ValueError, match="unknown config section"):
        cfg.section("nosuchstage")


def test_section_must_be_a_mapping(tmp_path):
    cfg = shared_config.load(_write(tmp_path, "batch: 42\n"))
    with pytest.raises(ConfigError, match="must be a mapping"):
        cfg.section("batch")


def test_sections_are_isolated(tmp_path):
    cfg = shared_config.load(
        _write(tmp_path, "pilot:\n  model: a\nbatch:\n  model: b\n"),
    )
    assert cfg.section("pilot")["model"] == "a"
    assert cfg.section("batch")["model"] == "b"


def test_require_names_the_section_and_key(tmp_path):
    cfg = shared_config.load(_write(tmp_path, "batch: {}\n"))
    with pytest.raises(ConfigError, match=r"batch\.api_url"):
        cfg.require("batch", "api_url")


def test_paths_resolve_against_config_not_cwd(tmp_path, monkeypatch):
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = config_dir / "config.yaml"
    path.write_text("paths:\n  jobs_dir: ./jobs\n", encoding="utf-8")

    monkeypatch.chdir(elsewhere)
    cfg = shared_config.load(path)

    assert cfg.path_value("pilot", "jobs_dir") == (config_dir / "jobs").resolve()


def test_section_path_overrides_shared_paths(tmp_path):
    cfg = shared_config.load(
        _write(tmp_path, "paths:\n  jobs_dir: ./shared\npilot:\n  jobs_dir: ./own\n"),
    )
    assert cfg.path_value("pilot", "jobs_dir") == (tmp_path / "own").resolve()


def test_path_value_returns_none_without_a_default(tmp_path):
    cfg = shared_config.load(_write(tmp_path, ""))
    assert cfg.path_value("pilot", "jobs_dir") is None
    assert cfg.path_value("pilot", "jobs_dir", "./fallback") == (tmp_path / "fallback").resolve()


def test_langs_fills_in_suffix_and_label(tmp_path):
    cfg = shared_config.load(_write(tmp_path, "langs:\n  en: {}\n  es:\n    file_suffix: _spa\n"))
    langs = cfg.langs()
    assert langs["en"]["file_suffix"] == "_en"
    assert langs["en"]["label"] == "EN"
    assert langs["es"]["file_suffix"] == "_spa"


def test_langs_entry_must_be_a_mapping(tmp_path):
    cfg = shared_config.load(_write(tmp_path, "langs:\n  en: nonsense\n"))
    with pytest.raises(ConfigError, match=r"langs\.en"):
        cfg.langs()


def test_telegram_prefix_falls_back_to_shared_then_default(tmp_path):
    default = shared_config.load(_write(tmp_path, ""))
    assert default.telegram_prefix("batch") == "mpt-autopilot"

    shared_config.clear_cache()
    shared = shared_config.load(
        _write(tmp_path, "telegram_prefix: farm\nbatch:\n  telegram_prefix: farm-batch\n"),
    )
    assert shared.telegram_prefix("uploader") == "farm"
    assert shared.telegram_prefix("batch") == "farm-batch"


def test_load_is_cached_per_path_and_reload_bypasses_it(tmp_path):
    path = _write(tmp_path, "batch:\n  api_url: first\n")
    first = shared_config.load(path)
    path.write_text("batch:\n  api_url: second\n", encoding="utf-8")

    assert shared_config.load(path) is first
    assert shared_config.load(path, reload=True).section("batch")["api_url"] == "second"


def test_env_beside_config_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("MPT_TEST_TOKEN", raising=False)
    (tmp_path / ".env").write_text("MPT_TEST_TOKEN=from-config-dir\n", encoding="utf-8")
    shared_config.load(_write(tmp_path, ""))

    import os

    assert os.environ["MPT_TEST_TOKEN"] == "from-config-dir"
