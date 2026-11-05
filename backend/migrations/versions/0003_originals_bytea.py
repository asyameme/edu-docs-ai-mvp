"""Перенос исходных байтов из прежнего тома в PostgreSQL без смены ID документов."""

import hashlib
from uuid import UUID

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "0003_originals_bytea"
down_revision = "0002_stage4_review"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    op.execute("ALTER TABLE source_documents ADD COLUMN content bytea")
    # ALTER TABLE удерживает блокировку до конца общей транзакции миграции.
    # Защита истории отключается только для переноса тех же проверенных байтов.
    op.execute("ALTER TABLE source_documents DISABLE TRIGGER immutable_documents")
    rows = (
        connection.execute(
            sa.text("SELECT id,storage_key,size_bytes,sha256 FROM source_documents ORDER BY id")
        )
        .mappings()
        .all()
    )
    root = get_settings().original_files_path
    for row in rows:
        try:
            path = root / str(UUID(row["storage_key"]))
            if path.stat().st_size != row["size_bytes"]:
                raise ValueError("size mismatch")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise ValueError("hash mismatch")
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Нельзя перенести оригинал {row['id']}: {error}") from error
        connection.execute(
            sa.text("UPDATE source_documents SET content=:content WHERE id=:id"),
            {"content": content, "id": row["id"]},
        )
    op.execute("ALTER TABLE source_documents ENABLE TRIGGER immutable_documents")
    op.execute("""ALTER TABLE source_documents
        ALTER COLUMN content SET NOT NULL,
        ADD CONSTRAINT original_content_size CHECK (octet_length(content)=size_bytes),
        ADD CONSTRAINT original_content_hash CHECK (encode(sha256(content),'hex')=sha256),
        DROP COLUMN storage_key""")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM source_documents)")).scalar():
        raise RuntimeError(
            "Откат потребует выгрузки оригиналов; автоматическая потеря байтов запрещена"
        )
    op.execute("""ALTER TABLE source_documents
        DROP CONSTRAINT original_content_hash, DROP CONSTRAINT original_content_size,
        DROP COLUMN content, ADD COLUMN storage_key text NOT NULL UNIQUE""")
