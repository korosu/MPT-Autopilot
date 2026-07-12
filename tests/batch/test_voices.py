"""Tests for engine/voices.py — voice alias resolution."""

from __future__ import annotations

from mpt_batch.engine import voices


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
    """Voice alias is resolved to voice_name."""
    pool = {"test_voice": {"tts_server": "edge", "voice_name": "en-US-JennyNeural"}}
    payload = {"voice": "test_voice", "topic": "Hello"}
    result = voices.resolve(payload, pool)
    assert result["tts_server"] == "edge"
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


def test_resolve_explicit_tts_server_warning(capsys):
    """Explicit tts_server override prints warning if differs from alias."""
    pool = {"my_voice": {"tts_server": "edge", "voice_name": "edge_voice"}}
    payload = {"voice": "my_voice", "tts_server": "gemini", "topic": "Hello"}
    result = voices.resolve(payload, pool)
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    # Voice name still takes precedence
    assert result["voice_name"] == "edge_voice"
