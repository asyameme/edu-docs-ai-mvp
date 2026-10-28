"""Проверка начальной схемы в настоящем PostgreSQL без изменения рабочих данных."""

import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated PostgreSQL"),
]

ROOT = Path(__file__).parents[1] / "migrations"
TABLES = {
    "programs",
    "program_versions",
    "source_documents",
    "version_documents",
    "processing_jobs",
}


def schema_definition(connection, schema):
    """Сравниваем колонки, ограничения, индексы и защиту неизменяемой истории."""
    queries = {
        "columns": """SELECT table_name,column_name,data_type,udt_name,is_nullable,column_default
            FROM information_schema.columns WHERE table_schema=:schema
            AND table_name<>'alembic_version' ORDER BY table_name,ordinal_position""",
        "constraints": """SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema
            AND c.relname<>'alembic_version' ORDER BY c.relname,k.conname""",
        "indexes": """SELECT tablename,indexname,indexdef FROM pg_indexes
            WHERE schemaname=:schema AND tablename<>'alembic_version'
            ORDER BY tablename,indexname""",
        "triggers": """SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",
        "functions": """SELECT p.proname,pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname=:schema ORDER BY p.proname""",
    }
    result = {
        key: [list(row) for row in connection.execute(sa.text(sql), {"schema": schema})]
        for key, sql in queries.items()
    }
    return json.dumps(result, sort_keys=True).replace(schema + ".", "")


def test_single_initial_migration_matches_working_schema_and_can_be_recreated():
    path = ROOT / "versions" / "0001_initial_schema.py"
    assert path.exists(), "Нужна одна начальная миграция итоговой схемы"
    script = ScriptDirectory(str(ROOT))
    assert [revision.revision for revision in script.walk_revisions()] == ["0001_initial_schema"]
    spec = importlib.util.spec_from_file_location("initial_schema", path)
    change = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(change)
    settings = get_settings()
    engine = sa.create_engine(
        sa.URL.create(
            "postgresql+psycopg",
            username=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
            host=settings.postgres_host,
            database=settings.postgres_db,
        )
    )
    schema = "migration_test_" + uuid4().hex
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                with Operations.context(MigrationContext.configure(connection)):
                    change.upgrade()
                    tables = set(sa.inspect(connection).get_table_names(schema=schema))
                    assert tables == TABLES
                    initial = schema_definition(connection, schema)
                    assert initial == schema_definition(connection, "public")
                    change.downgrade()
                    assert sa.inspect(connection).get_table_names(schema=schema) == []
                    change.upgrade()
                    assert schema_definition(connection, schema) == initial
            finally:
                # Тестовые таблицы и все изменения откатываются одной транзакцией.
                transaction.rollback()
    finally:
        engine.dispose()
