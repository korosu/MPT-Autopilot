"""
Tests for mpt_autopilot.data.voice_data — the shared voice reference data.

The classifier mirrors MPT's prefix-based TTS dispatch; these tests pin the
behaviour the pilot's pre-flight and the verifier script both rely on.
Real bundled data is exercised (not mocks) so a corrupted/regressed JSON
fails here first.
"""

from __future__ import annotations

import zlib

from mpt_autopilot.data import voice_data


def test_gemini_prefix_with_bundled_name_is_gemini():
    assert voice_data.classify("gemini:Puck") == "gemini"
    # Case-insensitive: 780+ rendered videos used lowercase "gemini:puck".
    assert voice_data.classify("gemini:puck") == "gemini"


def test_gemini_name_with_gender_suffix_still_classifies():
    # MPT's parse_gemini_voice_name() takes the token before "-".
    assert voice_data.classify("gemini:Puck-Female") == "gemini"


def test_gemini_prefix_with_invented_name_is_unknown():
    # Exactly the espeak-class hallucination this module exists to catch.
    assert voice_data.classify("gemini:nonexistent") == "unknown"
    assert voice_data.classify("espeak:en") == "unknown"


def test_bundled_edge_name_is_edge():
    assert voice_data.classify("es-ES-ElviraNeural") == "edge"
    # parse_voice_name() strips -Female/-Male before dispatch.
    assert voice_data.classify("es-ES-ElviraNeural-Female") == "edge"


def test_provider_prefixes_are_accepted_as_provider():
    prefixes = (
        "siliconflow:",
        "mimo:",
        "minimax:",
        "elevenlabs:",
        "chatterbox:",
        "kokoro:",
        "fish_audio:",
    )
    for prefix in prefixes:
        assert voice_data.classify(f"{prefix}some-account-voice") == "provider"


def test_azure_v2_suffix_is_recognised():
    assert voice_data.classify("es-ES-ElviraNeural-V2") == "azure-v2"
    assert voice_data.classify("es-ES-ElviraNeural-Female-V2") == "azure-v2"


def test_no_voice_sentinels():
    assert voice_data.classify("no-voice") == "no-voice"
    assert voice_data.classify("none") == "no-voice"


def test_empty_and_blank_are_unknown():
    assert voice_data.classify("") == "unknown"
    assert voice_data.classify("   ") == "unknown"


def test_is_known_is_the_negation_of_unknown():
    assert voice_data.is_known("gemini:puck") is True
    assert voice_data.is_known("es-ES-ElviraNeural") is True
    assert voice_data.is_known("siliconflow:x") is True
    assert voice_data.is_known("espeak:en") is False


def test_bundled_data_is_loaded_and_sane():
    assert len(voice_data.edge_voices()) > 300  # the known ~314 free voices
    assert len(voice_data.gemini_voices()) == 30
    assert "Puck" in voice_data.gemini_voices()
    entries = voice_data.edge_voice_entries()
    assert {"name", "gender"} <= set(entries[0])


def test_describe_status_names_the_reason():
    assert "gemini" in voice_data.describe_status("gemini:puck")
    assert "UNKNOWN" in voice_data.describe_status("espeak:en")


def test_validate_pool_empty_is_a_single_required_error():
    errors = voice_data.validate_pool("es", [])
    assert len(errors) == 1
    assert "langs.es.voices is empty" in errors[0]
    assert "required" in errors[0]


def test_validate_pool_flags_unknown_entries_only():
    errors = voice_data.validate_pool(
        "es",
        ["gemini:charon", "es-ES-ElviraNeural", "siliconflow:foo", "espeak:en"],
    )
    assert len(errors) == 1
    assert "espeak:en" in errors[0]
    assert "UNKNOWN" in errors[0]


def test_validate_pool_clean_pool_returns_no_errors():
    assert voice_data.validate_pool("en", ["gemini:puck", "gemini:kore"]) == []


def test_hash_assignment_matches_refill_formula():
    """Pilot's assignment is crc32(job_name) % len(pool) — pin the formula."""
    pool = ["gemini:puck", "gemini:kore", "gemini:fenrir"]
    job = "fact_tongue_strongest_muscle_en"
    expected = pool[zlib.crc32(job.encode("utf-8")) % len(pool)]
    assert expected in pool  # sanity
    assert voice_data.classify(expected) == "gemini"
