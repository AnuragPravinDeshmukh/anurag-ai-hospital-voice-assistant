"""
Unit tests for Mock Knowledge Retrieval and RAG fallback behavior
Tests local RAG keyword classification and behavior when external Google Gemini credentials are absent.
"""
import pytest
from unittest.mock import patch, MagicMock
from modules.google_file_search import (
    search_knowledge_base,
    get_relevant_context,
    check_file_search_status
)


def test_file_search_status_without_key():
    """Verify check_file_search_status safely reports lack of API key without crashing."""
    status = check_file_search_status()
    assert isinstance(status, dict)
    assert "sdk_available" in status
    assert "api_key_set" in status
    assert "initialized" in status


def test_search_knowledge_base_handles_missing_client():
    """Verify search gracefully returns error dictionary when client is uninitialized."""
    with patch("modules.google_file_search.get_client", return_value=None):
        result = search_knowledge_base("What are doctor consultation charges?")
        assert result.get("answer") is None
        assert "error" in result


def test_search_knowledge_base_mock_success():
    """Verify mocked Google Gemini File Search response parsing."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "General physician consultation is ₹300-500. Specialists are ₹500-800."
    mock_response.candidates = []
    mock_client.models.generate_content.return_value = mock_response

    with patch("modules.google_file_search.get_client", return_value=mock_client), \
         patch("modules.google_file_search.get_or_create_file_search_store", return_value="test-store"):
        
        result = search_knowledge_base("consultation fees")
        assert result.get("answer") is not None
        assert "₹300-500" in result["answer"]
        assert "search_time" in result


def test_get_relevant_context_truncation():
    """Verify get_relevant_context limits response length to max_chars."""
    with patch("modules.google_file_search.search_knowledge_base", return_value={"answer": "A" * 500}):
        context = get_relevant_context("query", max_chars=100)
        assert len(context) <= 104  # 100 chars + "..."
        assert context.endswith("...")
