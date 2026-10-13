"""Короткие транзакции PostgreSQL; без работы с файлами и Temporal при блокировке строк."""

import hashlib
import json
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain import Snapshot
from app.errors import DomainError

PROGRAM_COLUMNS = "id, name, current_version_id, created_at"
VERSION_COLUMNS = """id, program_id, number, parent_version_id, restored_from_version_id,
                     origin, created_at, snapshot"""
DOCUMENT_COLUMNS = """id, program_id, original_name, format, media_type, size_bytes,
                      sha256, created_at"""


class Repository:
    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def require(row):
        if row is None:
            raise DomainError(404, "Object not found in this program")
        return row

    def create_program(self, name):
        program_id, version_id = uuid4(), uuid4()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("INSERT INTO programs(id, name) VALUES (%s, %s)", (program_id, name))
            state = Snapshot().model_dump(mode="json")
            cursor.execute(
                """INSERT INTO program_versions(id, program_id, number, snapshot, origin)
                   VALUES (%s, %s, 1, %s, 'initial')""",
                (version_id, program_id, Jsonb(state)),
            )
            cursor.execute(
                """UPDATE programs SET current_version_id = %s WHERE id = %s
                   RETURNING """
                + PROGRAM_COLUMNS,
                (version_id, program_id),
            )
            return cursor.fetchone()

    def program(self, program_id):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT " + PROGRAM_COLUMNS + " FROM programs WHERE id = %s", (program_id,)
            )
            return self.require(cursor.fetchone())

    def programs(self, limit=50, offset=0):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT "
                + PROGRAM_COLUMNS
                + " FROM programs ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return cursor.fetchall()

    def version(self, program_id, version_id):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT "
                + VERSION_COLUMNS
                + " FROM program_versions WHERE program_id=%s AND id=%s",
                (program_id, version_id),
            )
            return self.require(cursor.fetchone())

    def versions(self, program_id, limit=50, offset=0):
        self.program(program_id)
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT "
                + VERSION_COLUMNS
                + """ FROM program_versions WHERE program_id=%s
                               ORDER BY number DESC LIMIT %s OFFSET %s""",
                (program_id, limit, offset),
            )
            return cursor.fetchall()

    def save(self, program_id, expected_version_id, key, snapshot=None, restore_id=None):
        request = {
            "operation": "restore" if restore_id else "save",
            "expected_version_id": str(expected_version_id),
            "restore_id": str(restore_id) if restore_id else None,
            "snapshot": snapshot.model_dump(mode="json") if snapshot is not None else None,
        }
        fingerprint = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute(
                "SELECT current_version_id FROM programs WHERE id=%s FOR UPDATE", (program_id,)
            )
            current = self.require(cursor.fetchone())["current_version_id"]
            cursor.execute(
                "SELECT "
                + VERSION_COLUMNS
                + " , request_hash FROM program_versions WHERE program_id=%s AND operation_key=%s",
                (program_id, key),
            )
            previous = cursor.fetchone()
            if previous is not None:
                if previous.pop("request_hash") != fingerprint:
                    raise DomainError(409, "Idempotency key was used for another request")
                return previous
            if current != expected_version_id:
                raise DomainError(409, "Current version changed; reload before saving")
            if restore_id:
                cursor.execute(
                    "SELECT snapshot FROM program_versions WHERE program_id=%s AND id=%s",
                    (program_id, restore_id),
                )
                snapshot = Snapshot.model_validate(self.require(cursor.fetchone())["snapshot"])
            cursor.execute(
                "SELECT number FROM program_versions WHERE program_id=%s AND id=%s",
                (program_id, current),
            )
            number = cursor.fetchone()["number"] + 1
            state = snapshot.model_dump(mode="json")
            if snapshot.document_ids:
                cursor.execute(
                    "SELECT id FROM source_documents WHERE program_id=%s AND id=ANY(%s)",
                    (program_id, snapshot.document_ids),
                )
                found = {row["id"] for row in cursor.fetchall()}
                if found != set(snapshot.document_ids):
                    raise DomainError(422, "Document is absent or belongs to another program")
            version_id = uuid4()
            cursor.execute(
                """INSERT INTO program_versions
                   (id, program_id, number, parent_version_id, restored_from_version_id,
                    snapshot, origin, operation_key, request_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING """
                + VERSION_COLUMNS,
                (
                    version_id,
                    program_id,
                    number,
                    current,
                    restore_id,
                    Jsonb(state),
                    "restore" if restore_id else "save",
                    key,
                    fingerprint,
                ),
            )
            result = cursor.fetchone()
            self.attach_documents(cursor, program_id, version_id, snapshot.document_ids)
            cursor.execute(
                "UPDATE programs SET current_version_id=%s WHERE id=%s", (version_id, program_id)
            )
            return result

    @staticmethod
    def attach_documents(cursor, program_id, version_id, document_ids):
        for document_id in document_ids:
            cursor.execute(
                """INSERT INTO version_documents(program_id, version_id, document_id)
                   VALUES (%s,%s,%s)""",
                (program_id, version_id, document_id),
            )

    def add_document(self, program_id, stored):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                """INSERT INTO source_documents
                   (id, program_id, original_name, format, media_type,
                    size_bytes, sha256, storage_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING """
                + DOCUMENT_COLUMNS,
                (
                    stored["id"],
                    program_id,
                    stored["original_name"],
                    stored["format"],
                    stored["media_type"],
                    stored["size_bytes"],
                    stored["sha256"],
                    stored["storage_key"],
                ),
            )
            return cursor.fetchone()

    def documents(self, program_id, limit=50, offset=0):
        self.program(program_id)
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT "
                + DOCUMENT_COLUMNS
                + """ FROM source_documents WHERE program_id=%s
                               ORDER BY created_at DESC, id LIMIT %s OFFSET %s""",
                (program_id, limit, offset),
            )
            return cursor.fetchall()

    def document(self, program_id, document_id):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT "
                + DOCUMENT_COLUMNS
                + " , storage_key FROM source_documents WHERE program_id=%s AND id=%s",
                (program_id, document_id),
            )
            return self.require(cursor.fetchone())
