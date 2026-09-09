"""
Tests for `mpt run`: stage/language selection, exit-code aggregation, and the
per-language isolation the pipeline promises.

Every test replaces the stage functions with fakes — the point here is the
orchestration, not the stages, which have their own tests.
"""

from __future__ import annotations

import argparse

import pytest

from mpt_autopilot import config as shared_config
from mpt_autopilot import pipeline
from mpt_autopilot.config import ConfigError
from mpt_autopilot.pipeline import EXIT_FAILURES, EXIT_OK, EXIT_QUOTA_STOP

CONFIG = """\
langs:
  en:
    file_suffix: ""
  es:
    file_suffix: _es
batch:
  output_dir: ./exports
"""


@pytest.fixture(autouse=True)
def _clear_config_cache():
    shared_config.clear_cache()
    yield
    shared_config.clear_cache()


def _config(tmp_path, text: str = CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        langs=None,
        all_langs=False,
        only=None,
        skip=None,
        dry_run=False,
        continue_on_error=False,
        limit=None,
        config=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_stages(monkeypatch, codes: dict[str, int], calls: list[tuple[str, str]]):
    """Replace every stage with a fake that records (lang, stage) and returns a code."""

    def make(stage: str):
        def fake(lang, config_path, args, *extra):
            calls.append((lang, stage))
            return codes.get(stage, EXIT_OK)

        return fake

    for stage, attr in (
        ("refill", "_run_refill"),
        ("batch", "_run_batch"),
        ("enrich", "_run_enrich"),
        ("upload", "_run_upload"),
    ):
        monkeypatch.setattr(pipeline, attr, make(stage))


# ── stage selection ───────────────────────────────────────────────────────────


def test_resolve_stages_defaults_to_all_in_order():
    assert pipeline.resolve_stages(None, None) == ["refill", "batch", "enrich", "upload"]


def test_resolve_stages_keeps_pipeline_order_regardless_of_flag_order():
    assert pipeline.resolve_stages(["upload", "refill"], None) == ["refill", "upload"]


def test_resolve_stages_skip():
    assert pipeline.resolve_stages(None, ["refill", "upload"]) == ["batch", "enrich"]


def test_resolve_stages_only_and_skip_combine():
    assert pipeline.resolve_stages(["batch", "enrich"], ["enrich"]) == ["batch"]


# ── language selection ────────────────────────────────────────────────────────


def test_resolve_langs_defaults_to_config_order(tmp_path):
    cfg = shared_config.load(_config(tmp_path))
    assert pipeline.resolve_langs(cfg, None) == ["en", "es"]


def test_resolve_langs_rejects_unknown(tmp_path):
    cfg = shared_config.load(_config(tmp_path))
    with pytest.raises(ConfigError, match="unknown language"):
        pipeline.resolve_langs(cfg, ["fr"])


def test_resolve_langs_deduplicates_and_keeps_request_order(tmp_path):
    cfg = shared_config.load(_config(tmp_path))
    assert pipeline.resolve_langs(cfg, ["es", "en", "es"]) == ["es", "en"]


# ── wiring ────────────────────────────────────────────────────────────────────


def test_exports_dir_follows_the_language_suffix(tmp_path):
    cfg = shared_config.load(_config(tmp_path))
    assert pipeline.exports_dir_for(cfg, "en") == (tmp_path / "exports").resolve()
    assert pipeline.exports_dir_for(cfg, "es") == (tmp_path / "exports_es").resolve()


def test_account_defaults_to_the_language_code(tmp_path):
    cfg = shared_config.load(_config(tmp_path))
    assert pipeline.account_for(cfg, "es") == "es"


def test_account_mapping_overrides_the_language_code(tmp_path):
    cfg = shared_config.load(
        _config(tmp_path, CONFIG + "pipeline:\n  accounts:\n    es: spanish-channel\n"),
    )
    assert pipeline.account_for(cfg, "es") == "spanish-channel"
    assert pipeline.account_for(cfg, "en") == "en"


def test_account_mapping_must_be_a_mapping(tmp_path):
    cfg = shared_config.load(_config(tmp_path, CONFIG + "pipeline:\n  accounts: nonsense\n"))
    with pytest.raises(ConfigError, match="pipeline.accounts"):
        pipeline.account_for(cfg, "en")


# ── orchestration ─────────────────────────────────────────────────────────────


def test_runs_every_stage_for_every_language(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {}, calls)
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports_es").mkdir()

    code = pipeline.execute(_args(), _config(tmp_path))

    assert code == EXIT_OK
    assert calls == [
        ("en", "refill"),
        ("en", "batch"),
        ("en", "enrich"),
        ("en", "upload"),
        ("es", "refill"),
        ("es", "batch"),
        ("es", "enrich"),
        ("es", "upload"),
    ]


def test_enrich_is_skipped_when_the_exports_dir_is_absent(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {}, calls)

    code = pipeline.execute(_args(langs=["en"], only=["enrich"]), _config(tmp_path))

    assert code == EXIT_OK
    assert calls == []  # nothing ran, and it was not treated as a failure


def test_failure_stops_the_language_by_default(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {"batch": EXIT_FAILURES}, calls)

    code = pipeline.execute(_args(langs=["en"]), _config(tmp_path))

    assert code == EXIT_FAILURES
    assert calls == [("en", "refill"), ("en", "batch")]


def test_continue_on_error_keeps_going(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {"batch": EXIT_FAILURES}, calls)
    (tmp_path / "exports").mkdir()

    code = pipeline.execute(_args(langs=["en"], continue_on_error=True), _config(tmp_path))

    assert code == EXIT_FAILURES
    assert [stage for _lang, stage in calls] == ["refill", "batch", "enrich", "upload"]


def test_one_language_failing_does_not_skip_the_next(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {"refill": EXIT_FAILURES}, calls)

    code = pipeline.execute(_args(), _config(tmp_path))

    assert code == EXIT_FAILURES
    assert calls == [("en", "refill"), ("es", "refill")]


def test_a_crashing_stage_becomes_a_failure_not_a_traceback(tmp_path, monkeypatch):
    def boom(lang, config_path, args, *extra):
        raise RuntimeError("network died")

    monkeypatch.setattr(pipeline, "_run_refill", boom)

    code = pipeline.execute(_args(langs=["en"], only=["refill"]), _config(tmp_path))

    assert code == EXIT_FAILURES


def test_quota_stop_is_reported_when_nothing_failed(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {"upload": EXIT_QUOTA_STOP}, calls)

    code = pipeline.execute(_args(langs=["en"], only=["upload"]), _config(tmp_path))

    assert code == EXIT_QUOTA_STOP


def test_failure_dominates_quota_stop(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {"upload": EXIT_QUOTA_STOP, "batch": EXIT_FAILURES}, calls)

    code = pipeline.execute(
        _args(only=["batch", "upload"], continue_on_error=True), _config(tmp_path)
    )

    assert code == EXIT_FAILURES


def test_partial_batch_continues_to_enrich_and_upload(tmp_path, monkeypatch):
    """batch returning 2 (partial success) must not stop the pipeline."""
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {"batch": EXIT_QUOTA_STOP}, calls)
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports_es").mkdir()

    code = pipeline.execute(_args(langs=["en"]), _config(tmp_path))

    assert code == EXIT_QUOTA_STOP
    assert ("en", "enrich") in calls
    assert ("en", "upload") in calls


def test_partial_then_partial_continues_to_upload(tmp_path, monkeypatch):
    """partial batch + partial enricher still lets upload run."""
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {"batch": EXIT_QUOTA_STOP, "enrich": EXIT_QUOTA_STOP}, calls)
    (tmp_path / "exports").mkdir()

    code = pipeline.execute(_args(langs=["en"]), _config(tmp_path))

    assert code == EXIT_QUOTA_STOP
    assert ("en", "enrich") in calls
    assert ("en", "upload") in calls


def test_dry_run_skips_refill(tmp_path, monkeypatch, capsys):
    calls: list[tuple[str, str]] = []
    _fake_stages(monkeypatch, {}, calls)

    pipeline.execute(_args(langs=["en"], dry_run=True), _config(tmp_path))

    assert ("en", "refill") not in calls
    assert "skipping refill" in capsys.readouterr().out


def test_no_langs_configured_is_an_error(tmp_path, capsys):
    code = pipeline.execute(_args(), _config(tmp_path, "batch: {}\n"))
    assert code == EXIT_FAILURES
    assert "no languages" in capsys.readouterr().out


def test_only_and_skip_cancelling_out_is_an_error(tmp_path, capsys):
    code = pipeline.execute(_args(only=["batch"], skip=["batch"]), _config(tmp_path))
    assert code == EXIT_FAILURES
    assert "no stages" in capsys.readouterr().out


def test_missing_config_is_reported_not_raised(tmp_path, capsys):
    code = pipeline.execute(_args(), tmp_path / "absent.yaml")
    assert code == EXIT_FAILURES
    assert "not found" in capsys.readouterr().out
