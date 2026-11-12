"""Атомарная запись версии графа, решений и предметных замечаний."""

from uuid import UUID

from psycopg.types.json import Jsonb

from app.errors import DomainError
from app.repository import VERSION_COLUMNS, version_dto


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
        return version_dto(row)


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
    actor_type="system",
    actor_id="backend",
):
    from app.graph_storage import journal, load_graph, save_graph

    state = snapshot.model_dump(mode="json")
    parent = cur.execute(
        "SELECT number,graph_state_id,review FROM program_versions WHERE program_id=%s AND id=%s",
        (program_id, parent_id),
    ).fetchone()
    if parent is None:
        raise DomainError(404, "Родительская версия не найдена")
    before = load_graph(cur, parent["graph_state_id"], parent["review"])
    if restore_id:
        graph = cur.execute(
            "SELECT graph_state_id FROM program_versions WHERE program_id=%s AND id=%s",
            (program_id, restore_id),
        ).fetchone()["graph_state_id"]
    else:
        graph = save_graph(cur, program_id, snapshot)
    row = cur.execute(
        "INSERT INTO "
        "program_versions(id,program_id,number,parent_version_id,restored_from_version_id,"
        "graph_state_id,review,origin,operation_key,request_hash) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING " + VERSION_COLUMNS,
        (
            version_id,
            program_id,
            parent["number"] + 1,
            parent_id,
            restore_id,
            graph,
            Jsonb(state["review"]) if state.get("review") else None,
            origin,
            key,
            fingerprint,
        ),
    ).fetchone()
    journal(
        cur,
        program_id,
        version_id,
        before,
        row["snapshot"],
        actor_type=actor_type,
        actor_id=actor_id,
    )
    cur.execute("UPDATE programs SET current_version_id=%s WHERE id=%s", (version_id, program_id))
    return version_dto(row)


def insert_issues(cur, program_id, job_id, version_id, issues):
    cur.execute(
        "UPDATE processing_jobs SET checked_version_id=%s WHERE id=%s", (version_id, job_id)
    )
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
