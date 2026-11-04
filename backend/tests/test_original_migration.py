"""Повреждение старого файла не оставляет частично перенесённую схему или байты."""

import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated PostgreSQL"),
]


@pytest.mark.parametrize("fault", ["missing", "size", "hash"])
def test_failed_original_migration_rolls_back_schema_and_trigger(tmp_path, monkeypatch, fault):
    root = Path(__file__).parents[1] / "migrations" / "versions"
    changes = []
    for name in ("0001_initial_schema", "0002_stage4_review", "0003_originals_bytea"):
        spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        changes.append(module)
    monkeypatch.setattr(
        changes[2], "get_settings", lambda: SimpleNamespace(original_files_path=tmp_path)
    )
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
    schema = "bytea_migration_" + uuid4().hex
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            with Operations.context(MigrationContext.configure(connection)):
                changes[0].upgrade()
                changes[1].upgrade()
                program = uuid4()
                connection.execute(
                    sa.text("INSERT INTO programs(id,name) VALUES(:id,'Migration')"),
                    {"id": program},
                )
                # Первый файл успевает перенестись; ошибка второго должна откатить и его.
                for i in (1, 2):
                    identity = f"00000000-0000-4000-8000-{i:012d}"
                    raw = b"original"
                    if i == 1 or fault != "missing":
                        (tmp_path / identity).write_bytes(
                            raw if i == 1 else (b"short" if fault == "size" else b"changed!")
                        )
                    connection.execute(
                        sa.text("""INSERT INTO source_documents
                        (id,program_id,original_name,format,media_type,size_bytes,sha256,storage_key)
                        VALUES(:id,:program,'source.pdf','pdf','application/pdf',8,:hash,:key)"""),
                        {
                            "id": identity,
                            "key": identity,
                            "program": program,
                            "hash": hashlib.sha256(raw).hexdigest(),
                        },
                    )
                with (
                    pytest.raises(RuntimeError, match="Нельзя перенести"),
                    connection.begin_nested(),
                ):
                    changes[2].upgrade()
                columns = {
                    c["name"]
                    for c in sa.inspect(connection).get_columns("source_documents", schema=schema)
                }
                assert "storage_key" in columns and "content" not in columns
                assert (
                    connection.exec_driver_sql("SELECT count(*) FROM source_documents").scalar()
                    == 2
                )
                with pytest.raises(sa.exc.DBAPIError), connection.begin_nested():
                    connection.exec_driver_sql(
                        "UPDATE source_documents SET original_name='changed'"
                    )
            transaction.rollback()
    finally:
        engine.dispose()
