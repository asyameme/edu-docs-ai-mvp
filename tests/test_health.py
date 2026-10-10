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
    http = MagicMock()
    http.get = AsyncMock(return_value=MagicMock())
    return Resources(MagicMock(), temporal, http, "http://chroma:8000")


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
        "chroma": "available",
    }


@pytest.mark.parametrize("dependency", ["postgres", "temporal", "chroma"])
def test_dependency_failure_does_not_break_liveness_or_leak_credentials(
    client, resources, dependency
):
    error = ConnectionError("private-password-do-not-leak")
    if dependency == "postgres":
        resources.postgres.connection.side_effect = error
    elif dependency == "temporal":
        resources.temporal.workflow_service.describe_namespace.side_effect = error
    else:
        resources.http.get.side_effect = error
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"][dependency] == "unavailable"
    assert "private-password" not in response.text
    assert client.get("/health/live").status_code == 200


def test_demo_preserves_both_conflicting_values_with_sources(client):
    data = client.get("/api/v1/demo/program").json()
    assert data["is_demo"] is True
    assert data["disciplines"][0]["hours"] is None
    options = data["conflicts"][0]["options"]
    assert {option["value"] for option in options} == {108, 144}
    sources = {source["id"] for source in data["source_refs"]}
    assert all(option["source_ref_id"] in sources for option in options)


@pytest.mark.parametrize("dependency", ["temporal", "chroma"])
def test_hanging_dependency_has_bounded_readiness(client, resources, dependency):
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    if dependency == "temporal":
        resources.temporal.workflow_service.describe_namespace.side_effect = hang
    else:
        resources.http.get.side_effect = hang
    started = time.monotonic()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"][dependency] == "unavailable"
    assert time.monotonic() - started < 5
    assert client.get("/health/live").status_code == 200


def test_http_error_from_chroma_marks_dependency_unavailable(client, resources):
    resources.http.get.return_value.raise_for_status.side_effect = ConnectionError(
        "private-address"
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"]["chroma"] == "unavailable"
    assert "private-address" not in response.text
