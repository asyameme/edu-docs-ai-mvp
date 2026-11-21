import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.health import Resources
from app.main import app


@pytest.fixture
def resources():
    temporal = MagicMock()
    temporal.namespace = "education"
    temporal.workflow_service.describe_namespace = AsyncMock()
    return Resources(MagicMock(), temporal)


@pytest.fixture
def client(resources, monkeypatch):
    # Без запуска lifespan: зависимости подставлены для проверки сценариев отказа.
    monkeypatch.setattr(app, "state", SimpleNamespace(resources=resources))
    return TestClient(app)


def test_ready_with_available_dependencies(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["dependencies"] == {
        "postgres": "available",
        "temporal": "available",
    }


@pytest.mark.parametrize("dependency", ["postgres", "temporal"])
def test_dependency_failure_does_not_break_liveness_or_leak_credentials(
    client, resources, dependency
):
    error = ConnectionError("private-password-do-not-leak")
    if dependency == "temporal":
        resources.temporal.workflow_service.describe_namespace.side_effect = error
    else:
        resources.postgres.connection.side_effect = error
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"][dependency] == "unavailable"
    assert "private-password" not in response.text
    assert client.get("/health/live").status_code == 200


def test_artificial_demo_is_not_exposed(client):
    assert client.get("/api/v1/demo/program").status_code == 404
    assert "/api/v1/demo/program" not in client.get("/openapi.json").json()["paths"]


def test_hanging_temporal_dependency_has_bounded_readiness(client, resources):
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    resources.temporal.workflow_service.describe_namespace.side_effect = hang
    started = time.monotonic()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"]["temporal"] == "unavailable"
    assert time.monotonic() - started < 5
    assert client.get("/health/live").status_code == 200
