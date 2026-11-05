"""Подготовка только двух баз Temporal в существующем томе PostgreSQL."""

from pathlib import Path

import psycopg
from psycopg import sql

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    if not settings.temporal_db_password.get_secret_value():
        raise ValueError("TEMPORAL_DB_PASSWORD must be configured")
    role = settings.temporal_db_user
    with psycopg.connect(
        host=settings.postgres_host,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
        autocommit=True,
    ) as admin:
        admin.execute("SELECT pg_advisory_lock(79002311)")
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role),
                    sql.Literal(settings.temporal_db_password.get_secret_value()),
                )
            )
        else:
            admin.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role),
                    sql.Literal(settings.temporal_db_password.get_secret_value()),
                )
            )
        for database in ("temporal", "temporal_visibility"):
            owner = admin.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                (database,),
            ).fetchone()
            if owner is None:
                admin.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(database), sql.Identifier(role)
                    )
                )
            elif owner[0] != role:
                raise RuntimeError("Refusing to change ownership of an existing Temporal database")
            with psycopg.connect(
                host=settings.postgres_host,
                dbname=database,
                user=role,
                password=settings.temporal_db_password.get_secret_value(),
                connect_timeout=5,
            ) as connection:
                table = connection.execute(
                    "SELECT to_regclass('public.schema_version')"
                ).fetchone()[0]
                initialized = (
                    table is not None
                    and connection.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 1
                )
            Path("/init-state/" + database + ".setup").write_text(
                "0" if initialized else "1", encoding="utf-8"
            )
    print("Temporal databases prepared; application database preserved")


if __name__ == "__main__":
    main()
