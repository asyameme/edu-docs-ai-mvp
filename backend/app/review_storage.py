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


def journal_before_snapshot(before, snapshot, state):
    """Сериализовать прежние значения только у изменившихся частей модели."""
    from app.domain import SnapshotV2

    if not isinstance(before, SnapshotV2) or not isinstance(snapshot, SnapshotV2):
        return before.model_dump(mode="json") if hasattr(before, "model_dump") else before
    shared = {
        field for field in type(snapshot).model_fields
        if getattr(before, field) is getattr(snapshot, field)
    }
    return {
        **before.model_dump(mode="json", exclude=shared),
        **{field: state[field] for field in shared},
    }


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
    reuse_parent_graph=False,
    before_snapshot=None,
):
    from app.domain import SnapshotV2
    from app.graph_storage import journal, load_graph, save_graph
    from app.review_logic import digest

    state = snapshot.model_dump(mode="json")
    before_data = journal_before_snapshot(before_snapshot, snapshot, state)
    parent = cur.execute(
        "SELECT number,graph_state_id,review FROM program_versions WHERE program_id=%s AND id=%s",
        (program_id, parent_id),
    ).fetchone()
    if parent is None:
        raise DomainError(404, "Родительская версия не найдена")
    if restore_id:
        before = before_data
        if before is None:
            before = load_graph(cur, parent["graph_state_id"], parent["review"])
        graph = cur.execute(
            "SELECT graph_state_id FROM program_versions WHERE program_id=%s AND id=%s",
            (program_id, restore_id),
        ).fetchone()["graph_state_id"]
    elif (state.get("review") or {}).get("graph_ready") is False:
        before = before_data
        if before is None:
            before = load_graph(cur, parent["graph_state_id"], parent["review"])
        graph = None
    elif reuse_parent_graph:
        if isinstance(before_snapshot, SnapshotV2) and isinstance(snapshot, SnapshotV2):
            graph_unchanged = all(
                getattr(before_snapshot, field) is getattr(snapshot, field)
                for field in (
                    "schema_version", "metadata", "document_ids", "entities", "relations",
                    "source_refs", "source_links",
                )
            )
            if not graph_unchanged:
                raise DomainError(
                    409, "Повторное использование требует неизменённой модели графа"
                )
            # Снимки разделяют те же объекты графа; в журнале меняется только review.
            before = {
                **state,
                "review": before_snapshot.review.model_dump(mode="json")
                if before_snapshot.review is not None
                else None,
            }
        else:
            before = before_data
            if before is None:
                before = load_graph(cur, parent["graph_state_id"], parent["review"])
            graph_unchanged = digest(
                {k: v for k, v in before.items() if k != "review"}
            ) == digest({k: v for k, v in state.items() if k != "review"})
        if parent["graph_state_id"] is None or not graph_unchanged:
            raise DomainError(409, "Повторное использование требует неизменного содержимого графа")
        graph = parent["graph_state_id"]
    else:
        before = before_data
        if before is None:
            before = load_graph(cur, parent["graph_state_id"], parent["review"])
        graph = save_graph(cur, program_id, snapshot)
    row = cur.execute(
        "INSERT INTO "
        "program_versions(id,program_id,number,parent_version_id,restored_from_version_id,"
        "graph_state_id,review,origin,operation_key,request_hash) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING "
        "id,program_id,number,parent_version_id,restored_from_version_id,origin,created_at",
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
    # state — та же проверенная модель, которую сохраняют graph_state_id/review.
    # Не перечитываем большой JSON-граф из PostgreSQL только ради журнала и ответа.
    row["snapshot"] = state
    journal(
        cur,
        program_id,
        version_id,
        before,
        state,
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
