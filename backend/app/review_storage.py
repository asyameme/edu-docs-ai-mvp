"""Транзакционная запись снимка и SQL-проекций его источников."""

from uuid import UUID

from psycopg.types.json import Jsonb

from app.errors import DomainError
from app.repository import VERSION_COLUMNS


def key_in_jobs(cur, program_id, key):
    return cur.execute(
        """SELECT id FROM processing_jobs WHERE program_id=%s AND
           (operation_key=%s OR parameters->'request_keys' ? %s)""",
        (program_id, key, key),
    ).fetchone()


def previous_version(cur, program_id, key, fingerprint):
    if key_in_jobs(cur, program_id, key):
        raise DomainError(409, "Ключ уже использован для фоновой задачи")
    row = cur.execute(
        "SELECT " + VERSION_COLUMNS + ",request_hash FROM program_versions "
        "WHERE program_id=%s AND operation_key=%s",
        (program_id, key),
    ).fetchone()
    if row:
        if row.pop("request_hash") != fingerprint:
            raise DomainError(409, "Ключ использован для другого запроса")
        return row


def insert_version(
    cur,
    program_id,
    parent_id,
    version_id,
    snapshot,
    *,
    key=None,
    fingerprint=None,
    origin="save",
    restore_id=None,
):
    state = snapshot.model_dump(mode="json")
    documents = cur.execute(
        "SELECT id FROM source_documents WHERE program_id=%s AND id=ANY(%s)",
        (program_id, snapshot.document_ids),
    ).fetchall()
    if {row["id"] for row in documents} != set(snapshot.document_ids):
        raise DomainError(422, "Источник отсутствует или принадлежит другой программе")
    row = cur.execute(
        """INSERT INTO program_versions(id,program_id,number,parent_version_id,
           restored_from_version_id,snapshot,origin,operation_key,request_hash)
           SELECT %s,%s,number+1,%s,%s,%s,%s,%s,%s FROM program_versions
           WHERE program_id=%s AND id=%s RETURNING """
        + VERSION_COLUMNS,
        (
            version_id,
            program_id,
            parent_id,
            restore_id,
            Jsonb(state),
            origin,
            key,
            fingerprint,
            program_id,
            parent_id,
        ),
    ).fetchone()
    for document_id in snapshot.document_ids:
        cur.execute(
            "INSERT INTO version_documents VALUES(%s,%s,%s)", (program_id, version_id, document_id)
        )
    from app.relational_storage import insert_projection

    insert_projection(cur, version_id, snapshot)
    cur.execute("UPDATE programs SET current_version_id=%s WHERE id=%s", (version_id, program_id))
    return row


def insert_issues(cur, program_id, job_id, version_id, issues):
    for issue in issues:
        cur.execute(
            """INSERT INTO validation_issues(id,program_id,job_id,version_id,rule_code,
               rule_revision,type,severity,status,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                UUID(issue["id"]),
                program_id,
                job_id,
                version_id,
                issue["rule_code"],
                issue["rule_revision"],
                issue["type"],
                issue["severity"],
                issue["status"],
                Jsonb(issue),
            ),
        )
