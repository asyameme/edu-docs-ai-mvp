"""Искусственные контрольные записи для проверки томов; без предметной схемы."""

import json
import sys
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

from app.config import get_settings


def main() -> None:
    action = sys.argv[1]
    settings = get_settings()
    if action == "prepare":
        token = uuid4().hex
        state = {
            "token": token,
            "table": "stage1_probe_" + token,
            "collection_name": "stage1-probe-" + token,
        }
    else:
        state = json.loads(sys.stdin.read().lstrip("\ufeff"))
        token = state["token"]
        if (
            state["table"] != "stage1_probe_" + token
            or state["collection_name"] != "stage1-probe-" + token
        ):
            raise ValueError("Invalid probe scope")
    with psycopg.connect(
        host=settings.postgres_host,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=3,
    ) as connection:
        with httpx.Client(base_url=settings.chroma_url, timeout=10, trust_env=False) as http:
            base = "/api/v2/tenants/default_tenant/databases/default_database/collections"
            table = sql.Identifier(state["table"])
            if action == "prepare":
                connection.execute(sql.SQL("CREATE TABLE {} (marker text NOT NULL)").format(table))
                connection.execute(
                    sql.SQL("INSERT INTO {} (marker) VALUES (%s)").format(table), (token,)
                )
                result = http.post(
                    base, json={"name": state["collection_name"], "get_or_create": False}
                )
                result.raise_for_status()
                state["collection_id"] = result.json()["id"]
                result = http.post(
                    base + "/" + state["collection_id"] + "/add",
                    json={
                        "ids": [token],
                        "embeddings": [[0.1, 0.2, 0.3]],
                        "metadatas": [{"probe": True}],
                        "documents": ["Artificial persistence probe"],
                    },
                )
                result.raise_for_status()
                print(json.dumps(state))
            elif action == "verify":
                row = connection.execute(sql.SQL("SELECT marker FROM {}").format(table)).fetchone()
                assert row[0] == token
                result = http.get(base + "/" + state["collection_name"])
                result.raise_for_status()
                assert result.json()["id"] == state["collection_id"]
                result = http.post(
                    base + "/" + state["collection_id"] + "/get", json={"ids": [token]}
                )
                result.raise_for_status()
                assert result.json()["ids"] == [token]
                print("Persistence passed: PostgreSQL, Chroma")
            elif action == "cleanup":
                connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
                result = http.delete(base + "/" + state["collection_name"])
                if result.status_code != 404:
                    result.raise_for_status()
                print("Artificial probe data removed")
            else:
                raise ValueError("Expected prepare, verify or cleanup")


if __name__ == "__main__":
    main()
