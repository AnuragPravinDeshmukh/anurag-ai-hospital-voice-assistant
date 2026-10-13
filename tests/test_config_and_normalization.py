"""
Unit tests for Configuration and Normalization Logic
Tests Anurag AI Hospital configuration defaults and TTS text normalizer.
"""
import pytest
import config
from realtime_app import normalize_text_for_tts, should_use_rag


def test_config_hospital_defaults():
    """Verify hospital branding and defaults."""
    assert config.HOSPITAL_NAME == "Anurag AI Hospital"
    assert "Anurag AI Hospital" in config.HOSPITAL_SYSTEM_PROMPT
    assert "General Physician" in config.AVAILABLE_DOCTORS
    assert config.DEFAULT_LANGUAGE == "ta-IN"
    assert "en-IN" in config.SUPPORTED_LANGUAGES


def test_tts_time_normalization_tamil():
    """Verify time formatting for Tamil TTS pronunciation."""
    input_text = "Doctor is available at 10:00 AM."
    normalized = normalize_text_for_tts(input_text, language="ta-IN")
    assert "10 மணி" in normalized


def test_tts_time_normalization_english():
    """Verify time formatting for English TTS pronunciation."""
    input_text = "Doctor is available at 10:00 AM."
    normalized = normalize_text_for_tts(input_text, language="en-IN")
    assert "10 o'clock AM" in normalized


def test_tts_abbreviations_expansion():
    """Verify medical abbreviations are expanded for clarity."""
    input_tamil = "OPD and ENT timing"
    normalized_ta = normalize_text_for_tts(input_tamil, language="ta-IN")
    assert "ஓ பி டி" in normalized_ta
    assert "இ என் டி" in normalized_ta

    input_english = "Visit OPD and ENT"
    normalized_en = normalize_text_for_tts(input_english, language="en-IN")
    assert "O P D" in normalized_en
    assert "E N T" in normalized_en


def test_should_use_rag():
    """Verify RAG keyword trigger detection."""
    # Pricing queries should trigger RAG
    assert should_use_rag("What is the consultation fee?", "en-IN") is True
    assert should_use_rag("எவ்வளவு கட்டணம்?", "ta-IN") is True

    # Simple greeting should not trigger RAG
    assert should_use_rag("Hello", "en-IN") is False
    assert should_use_rag("வணக்கம்", "ta-IN") is False
