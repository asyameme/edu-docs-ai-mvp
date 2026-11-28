"""Короткие транзакции PostgreSQL; без работы с файлами и Temporal при блокировке строк."""

import hashlib
import json
from uuid import uuid4

from psycopg.rows import dict_row

from app.domain import SnapshotV2
from app.errors import DomainError

PROGRAM_COLUMNS = "id, name, signature, current_version_id, created_at"
VERSION_COLUMNS = """id, program_id, number, parent_version_id, restored_from_version_id,
                     origin, created_at, graph_snapshot(graph_state_id,review) AS snapshot"""
DOCUMENT_COLUMNS = """id, program_id, original_name, format, media_type, size_bytes,
                      sha256, created_at"""
# Лимит 5 с защищает очередь к занятой программе: пул соединений API мал, и
# ждущие запросы не должны исчерпать его. Работа после захвата блокировки
# (сборка полного графа, запись пакета оригиналов) идёт уже без конкуренции
# и занимает десятки секунд — ей нужен отдельный, более широкий лимит.
LOCK_WAIT = "5s"
LOCKED_WORK = "120s"


def limit_lock_wait(cursor):
    """Ограничить ожидание блокировки программы, но не запись после неё."""
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", (LOCK_WAIT,))
    cursor.execute("SELECT set_config('statement_timeout', %s, true)", (LOCKED_WORK,))


def version_dto(row):
    """Адаптация старого хранения к V2 не меняет внешний контракт или историю."""
    if row and row["snapshot"].get("schema_version") == 1:
        return {
            **row,
            "snapshot": SnapshotV2.model_validate(
                {**row["snapshot"], "schema_version": 2}
            ).model_dump(mode="json"),
        }
    return row


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
            state = SnapshotV2().model_dump(mode="json")
            from app.graph_storage import save_graph

            graph = save_graph(cursor, program_id, state)
            cursor.execute(
                """INSERT INTO program_versions(id, program_id, number, graph_state_id, origin)
                   VALUES (%s, %s, 1, %s, 'initial')""",
                (version_id, program_id, graph),
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

    def update_signature(self, program_id, value):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute(
                "UPDATE programs SET signature = %s WHERE id = %s RETURNING " + PROGRAM_COLUMNS,
                (value, program_id),
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
            return version_dto(self.require(cursor.fetchone()))

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
            return [version_dto(row) for row in cursor.fetchall()]

    def save(
        self, program_id, expected_version_id, key, snapshot=None, restore_id=None, signature=None
    ):
        request = {
            "operation": "restore" if restore_id else "save",
            "expected_version_id": str(expected_version_id),
            "restore_id": str(restore_id) if restore_id else None,
            "snapshot": snapshot.model_dump(mode="json") if snapshot is not None else None,
            "signature": signature,
        }
        fingerprint = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            limit_lock_wait(cursor)
            cursor.execute(
                "SELECT current_version_id FROM programs WHERE id=%s FOR UPDATE", (program_id,)
            )
            current = self.require(cursor.fetchone())["current_version_id"]
            if cursor.execute(
                """SELECT id FROM processing_jobs WHERE program_id=%s AND
                   (operation_key=%s OR parameters->'request_keys' ? %s)""",
                (program_id, key, key),
            ).fetchone():
                raise DomainError(409, "Ключ использован для фоновой задачи")
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
                return version_dto(previous)
            if current != expected_version_id:
                raise DomainError(409, "Current version changed; reload before saving")
            current_snapshot = self.require(
                cursor.execute(
                    "SELECT graph_snapshot(graph_state_id,review) AS snapshot FROM "
                    "program_versions WHERE program_id=%s AND id=%s",
                    (program_id, current),
                ).fetchone()
            )["snapshot"]
            if (current_snapshot.get("review") or {}).get("graph_ready") is False:
                raise DomainError(409, "Нельзя изменять программу до завершения согласования")
            if restore_id:
                cursor.execute(
                    "SELECT graph_snapshot(graph_state_id,review) AS snapshot FROM "
                    "program_versions WHERE program_id=%s AND id=%s",
                    (program_id, restore_id),
                )
                restored = self.require(cursor.fetchone())["snapshot"]
                if (restored.get("review") or {}).get("graph_ready") is False:
                    raise DomainError(409, "Нельзя восстановить версию до сборки её графа")
                from app.review_storage import insert_version

                has_review = bool(restored.get("review"))
                restored = {**restored, "schema_version": 2, "review": (
                    {**restored["review"], "confirmation": None} if has_review else None
                )}
                version_id = uuid4()
                snapshot = SnapshotV2.model_validate(restored)
                version = insert_version(
                    cursor,
                    program_id,
                    current,
                    version_id,
                    snapshot,
                    key=key,
                    fingerprint=fingerprint,
                    origin="restore",
                    restore_id=restore_id,
                    actor_type="user" if signature else "system",
                    actor_id=signature or "backend",
                )
                if has_review:
                    from app.review_contract import CheckCreate
                    from app.review_repository import ReviewRepository

                    ReviewRepository(self.pool).create(
                        program_id, "automatic-check:" + str(version_id),
                        CheckCreate(version_id=version_id), purpose="check",
                        review_id=snapshot.review.review_id, connection=connection,
                        snapshot_override=snapshot,
                    )
                return version
            from app.review_storage import insert_version

            return insert_version(
                cursor,
                program_id,
                current,
                uuid4(),
                snapshot,
                key=key,
                fingerprint=fingerprint,
                actor_type="user" if signature else "system",
                actor_id=signature or "backend",
            )

    def add_document(self, program_id, stored):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return self.insert_document(cursor, program_id, stored)

    @staticmethod
    def insert_document(cursor, program_id, stored):
        cursor.execute(
            """INSERT INTO source_documents
                   (id, program_id, original_name, format, media_type,
                    size_bytes, sha256, content)
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
                stored["content"],
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
                + " FROM source_documents WHERE program_id=%s AND id=%s",
                (program_id, document_id),
            )
            return self.require(cursor.fetchone())

    def document_content(self, program_id, document_id):
        with (
            self.pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return self.require(
                cursor.execute(
                    "SELECT " + DOCUMENT_COLUMNS + ",content FROM source_documents "
                    "WHERE program_id=%s AND id=%s",
                    (program_id, document_id),
                ).fetchone()
            )
