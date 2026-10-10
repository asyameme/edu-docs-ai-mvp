import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import httpx
from psycopg_pool import ConnectionPool
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

from app.models import ReadyResponse

logger = logging.getLogger(__name__)


@dataclass
class Resources:
    postgres: ConnectionPool
    temporal: Client
    http: httpx.AsyncClient
    chroma_url: str


async def check_dependencies(resources: Resources) -> ReadyResponse:
    def postgres_check() -> None:
        with resources.postgres.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    async def temporal_check() -> None:
        # Настоящий RPC проверяет доступность сервиса и настроенного пространства имён.
        await resources.temporal.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=resources.temporal.namespace),
            timeout=timedelta(seconds=2),
        )

    async def chroma_check() -> None:
        response = await resources.http.get(f"{resources.chroma_url}/api/v2/heartbeat")
        response.raise_for_status()

    async def probe(name, check) -> tuple[str, Literal["available", "unavailable"]]:
        try:
            await asyncio.wait_for(check(), timeout=3)
        except Exception as error:
            logger.warning("Dependency %s unavailable (%s)", name, type(error).__name__)
            return name, "unavailable"
        return name, "available"

    async def postgres_async() -> None:
        await asyncio.to_thread(postgres_check)

    states = dict(
        await asyncio.gather(
            probe("postgres", postgres_async),
            probe("temporal", temporal_check),
            probe("chroma", chroma_check),
        )
    )
    return ReadyResponse(
        status="ready" if all(value == "available" for value in states.values()) else "not_ready",
        dependencies=states,
    )
