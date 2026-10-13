"""
Unit tests for Appointment Extraction and Validation Logic
Tests regex extraction fallback, validation guards, and Google Sheets save requirements.
"""
import pytest
from modules.google_sheets import (
    extract_name_from_text,
    extract_appointment_info_regex,
    save_appointment_to_sheets
)


def test_extract_name_from_english_text():
    """Test extracting English patient names."""
    text1 = "My name is John Doe, I need a consultation."
    assert extract_name_from_text(text1) == "John Doe"

    text2 = "I am Priya Sharma"
    assert extract_name_from_text(text2) == "Priya Sharma"


def test_extract_appointment_info_regex_fallback():
    """Test regex extraction of doctor, date, time, and patient name."""
    conversation = [
        {"role": "user", "content": "My name is Ramesh Kumar."},
        {"role": "assistant", "content": "Hello Ramesh. What symptoms do you have?"},
        {"role": "user", "content": "I have severe tooth pain and need a dentist tomorrow at 10 AM."}
    ]
    extracted = extract_appointment_info_regex(conversation)
    assert extracted["name"] == "Ramesh Kumar"
    assert extracted["doctor"] == "Dentist"
    assert "10:00 AM" in extracted["date_time"]


def test_save_appointment_to_sheets_validation_blocks_incomplete():
    """Ensure incomplete appointments (missing name or doctor) are rejected and not saved."""
    incomplete_appointment = {
        "name": "",
        "doctor": "Dentist",
        "date_time": "2026-10-15 10:00 AM"
    }
    # Should return False and not attempt to send incomplete booking
    assert save_appointment_to_sheets(incomplete_appointment) is False

    incomplete_no_doctor = {
        "name": "Anurag Deshmukh",
        "doctor": "",
        "date_time": "2026-10-15 10:00 AM"
    }
    assert save_appointment_to_sheets(incomplete_no_doctor) is False
