"""Проверка сохранности схемы и истории Temporal при повторной миграции приложения."""

import asyncio
import json
import sys

import psycopg
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

from app.config import get_settings


async def main():
    settings = get_settings()
    schemas = {}
    for database in ("temporal", "temporal_visibility"):
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=database,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as connection:
            schemas[database] = list(
                connection.execute(
                    "SELECT curr_version, min_compatible_version FROM schema_version"
                ).fetchone()
            )
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    result = await client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=settings.temporal_namespace)
    )
    state = {"schemas": schemas, "namespace_id": result.namespace_info.id}
    if sys.argv[1] == "prepare":
        print(json.dumps(state))
    else:
        expected = json.loads(sys.stdin.read().lstrip("\ufeff"))
        assert state == expected, (state, expected)
        print("Temporal schemas and namespace identity preserved")


if __name__ == "__main__":
    asyncio.run(main())
