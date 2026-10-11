"""Tests for engine/voices.py — voice alias resolution."""

from __future__ import annotations

from mpt_autopilot.batch import voices


def test_alias_for_edge_voice():
    """Normalize Edge TTS names to aliases."""
    assert voices.alias_for_edge_voice("es-ES-ElviraNeural") == "es_es_elvira"
    assert voices.alias_for_edge_voice("en-US-JennyNeural") == "en_us_jenny"


def test_resolve_no_voice():
    """Payload without voice key passes through unchanged."""
    payload = {"topic": "Hello"}
    result = voices.resolve(payload, {})
    assert result == {"topic": "Hello"}


def test_resolve_with_voice():
    """Voice alias resolves to voice_name; tts_server stays a pool-only label
    (B-3: VideoParams has no tts_server field, so it must not reach payload)."""
    pool = {"test_voice": {"tts_server": "edge", "voice_name": "en-US-JennyNeural"}}
    payload = {"voice": "test_voice", "topic": "Hello"}
    result = voices.resolve(payload, pool)
    assert "tts_server" not in result
    assert result["voice_name"] == "en-US-JennyNeural"
    assert "voice" not in result


def test_resolve_duplicate_alias_error():
    """Duplicate aliases raise ValueError."""
    # This tests the logic in build_pool, so we call it directly
    voices_cfg = {"gemini": {"dup": {}}, "edge": {"dup": {}}}
    try:
        voices.build_pool(voices_cfg)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "dup" in str(e)


def test_resolve_unknown_voice_error():
    """Unknown voice alias raises KeyError."""
    pool = {"known_voice": {"voice_name": "test"}}
    payload = {"voice": "unknown", "topic": "Hello"}
    try:
        voices.resolve(payload, pool)
        assert False, "Should have raised KeyError"
    except KeyError as e:
        assert "unknown" in str(e)


def test_resolve_preset_tts_server_never_reaches_payload(capsys):
    """B-3: an explicit tts_server in the job is neither overwritten nor
    merged from the preset — the MPT generation schema has no such field,
    and the old conflict warning implied the field mattered."""
    pool = {"my_voice": {"tts_server": "edge", "voice_name": "edge_voice"}}
    payload = {"voice": "my_voice", "tts_server": "gemini", "topic": "Hello"}
    result = voices.resolve(payload, pool)
    captured = capsys.readouterr()
    assert captured.out == ""  # no cosmetic-conflict warning anymore
    assert result["voice_name"] == "edge_voice"
    # A hand-written tts_server passes through untouched (it is dropped by
    # pydantic's extra='ignore' on MPT's side either way, silently).
    assert result["tts_server"] == "gemini"
