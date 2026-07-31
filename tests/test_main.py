"""Smoke tests for FastAPI application."""

import pytest
from fastapi.testclient import TestClient

from axe.config import Settings
from axe.main import create_app


@pytest.fixture
def client() -> TestClient:
    """Return a TestClient with test settings."""
    settings = Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:")
    app = create_app(settings=settings)
    return TestClient(app)


def test_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["app"] == "AXE"


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["env"] == "test"


def test_ready(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
