"""Проверка начальной схемы в настоящем PostgreSQL без изменения рабочих данных."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
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


def test_initial_and_stage4_migrations_preserve_v1_and_match_latest_schema(tmp_path, monkeypatch):
    path = ROOT / "versions" / "0001_initial_schema.py"
    assert path.exists(), "Нужна одна начальная миграция итоговой схемы"
    script = ScriptDirectory(str(ROOT))
    assert [revision.revision for revision in script.walk_revisions()] == [
        "0003_originals_bytea",
        "0002_stage4_review",
        "0001_initial_schema",
    ]
    spec = importlib.util.spec_from_file_location("initial_schema", path)
    change = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(change)
    spec3 = importlib.util.spec_from_file_location(
        "originals_bytea", ROOT / "versions" / "0003_originals_bytea.py"
    )
    binary = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(binary)
    monkeypatch.setattr(
        binary, "get_settings", lambda: SimpleNamespace(original_files_path=tmp_path)
    )
    (tmp_path / "55555555-5555-4555-8555-555555555555").write_bytes(b"original-raw")
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
                    change.downgrade()
                    assert sa.inspect(connection).get_table_names(schema=schema) == []
                    change.upgrade()
                    assert schema_definition(connection, schema) == initial
                    connection.exec_driver_sql("""
                        INSERT INTO programs(id,name) VALUES
                            ('11111111-1111-4111-8111-111111111111','Keep program');
                        INSERT INTO program_versions(id,program_id,number,snapshot,origin) VALUES
                            ('22222222-2222-4222-8222-222222222222',
                             '11111111-1111-4111-8111-111111111111',1,
                             '{"schema_version":1,"metadata":{"keep":"history"}}','initial');
                        UPDATE programs SET
                            current_version_id='22222222-2222-4222-8222-222222222222';
                        INSERT INTO source_documents(
                            id,program_id,original_name,format,media_type,size_bytes,sha256,storage_key)
                        VALUES('33333333-3333-4333-8333-333333333333',
                            '11111111-1111-4111-8111-111111111111','original.docx','docx',
                            'application/octet-stream',12,
                            encode(sha256(convert_to('original-raw','UTF8')),'hex'),
                            '55555555-5555-4555-8555-555555555555');
                        INSERT INTO version_documents VALUES(
                            '11111111-1111-4111-8111-111111111111',
                            '22222222-2222-4222-8222-222222222222',
                            '33333333-3333-4333-8333-333333333333');
                    """)
                    spec2 = importlib.util.spec_from_file_location(
                        "stage4", ROOT / "versions" / "0002_stage4_review.py"
                    )
                    stage4 = importlib.util.module_from_spec(spec2)
                    spec2.loader.exec_module(stage4)
                    stage4.upgrade()
                    assert set(sa.inspect(connection).get_table_names(schema=schema)) == TABLES | {
                        "document_artifacts",
                        "source_refs",
                        "source_links",
                        "validation_issues",
                    }
                    with connection.begin_nested() as trial:
                        binary.upgrade()
                        assert schema_definition(connection, schema) == schema_definition(
                            connection, "public"
                        )
                        assert (
                            bytes(
                                connection.exec_driver_sql(
                                    "SELECT content FROM source_documents"
                                ).scalar()
                            )
                            == b"original-raw"
                        )
                        with pytest.raises(RuntimeError, match="Откат"), connection.begin_nested():
                            binary.downgrade()
                        trial.rollback()
                    assert connection.exec_driver_sql(
                        "SELECT snapshot FROM program_versions"
                    ).scalar()["metadata"] == {"keep": "history"}
                    assert (
                        connection.exec_driver_sql(
                            "SELECT storage_key FROM source_documents"
                        ).scalar()
                        == "55555555-5555-4555-8555-555555555555"
                    )
                    stage4.downgrade()
                    assert schema_definition(connection, schema) == initial
                    stage4.upgrade()
                    with connection.begin_nested() as trial:
                        binary.upgrade()
                        assert schema_definition(connection, schema) == schema_definition(
                            connection, "public"
                        )
                        trial.rollback()
                    with pytest.raises(sa.exc.DBAPIError), connection.begin_nested():
                        connection.exec_driver_sql("""
                            INSERT INTO processing_jobs(id,program_id,input_version_id,
                                operation_key,request_hash,type,temporal_namespace,workflow_id)
                            VALUES('44444444-4444-4444-8444-444444444444',
                                '11111111-1111-4111-8111-111111111111',
                                '22222222-2222-4222-8222-222222222222',
                                'keep-validation-job','keep-hash','validate','education','keep-workflow');
                        """)
                        stage4.downgrade()
                    assert set(sa.inspect(connection).get_table_names(schema=schema)) == TABLES | {
                        "document_artifacts",
                        "source_refs",
                        "source_links",
                        "validation_issues",
                    }
            finally:
                # Тестовые таблицы и все изменения откатываются одной транзакцией.
                transaction.rollback()
    finally:
        engine.dispose()
