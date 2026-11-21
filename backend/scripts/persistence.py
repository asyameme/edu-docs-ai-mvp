"""Искусственные контрольные записи для проверки томов; без предметной схемы."""

import json
import sys
from uuid import uuid4

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
        }
    else:
        state = json.loads(sys.stdin.read().lstrip("\ufeff"))
        token = state["token"]
        if state["table"] != "stage1_probe_" + token:
            raise ValueError("Invalid probe scope")
    with psycopg.connect(
        host=settings.postgres_host,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=3,
    ) as connection:
        table = sql.Identifier(state["table"])
        if action == "prepare":
            connection.execute(sql.SQL("CREATE TABLE {} (marker text NOT NULL)").format(table))
            connection.execute(
                sql.SQL("INSERT INTO {} (marker) VALUES (%s)").format(table), (token,)
            )
            print(json.dumps(state))
        elif action == "verify":
            row = connection.execute(sql.SQL("SELECT marker FROM {}").format(table)).fetchone()
            assert row[0] == token
            print("Persistence passed: PostgreSQL")
        elif action == "cleanup":
            connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
            print("Artificial probe data removed")
        else:
            raise ValueError("Expected prepare, verify or cleanup")


if __name__ == "__main__":
    main()
