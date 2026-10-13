"""
Unit tests for FastAPI endpoints
Tests health-check, root, scheduler status, and input validation without requiring live credentials.
"""
import pytest
from fastapi.testclient import TestClient
from realtime_app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_root_endpoint(client):
    """Test root endpoint returns correct branding and active status."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert "Anurag AI Hospital" in data["project"]
    assert data["hospital"] == "Anurag AI Hospital"


def test_health_endpoint(client):
    """Test health endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["mode"] == "realtime-streaming"


def test_scheduler_status_endpoint(client):
    """Test background scheduler status endpoint (handles both active and disabled states)."""
    response = client.get("/scheduler/status")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    if data["status"] == "disabled":
        assert "reason" in data
    else:
        assert "jobs" in data


def test_schedule_call_missing_input(client):
    """Test validation when schedule_call is called with empty or invalid payload."""
    response = client.post("/schedule_call", json={})
    assert response.status_code in [400, 422]


def test_scheduled_calls_listing(client):
    """Test listing scheduled calls gracefully returns empty list when DB is offline."""
    response = client.get("/scheduled_calls")
    assert response.status_code == 200
    data = response.json()
    assert "scheduled_calls" in data
    assert isinstance(data["scheduled_calls"], list)
