"""Бизнес-запросы и результаты импорта; состояние выполнения хранит Temporal."""

from datetime import datetime
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.errors import DomainError
from app.graph_storage import hydrate_report, load_graph, parsed_artifact, save_graph
from app.repository import Repository


def workflow_identity(job_id, attempt):
    return f"import-{job_id}-{attempt}"


def workflow_attempt(workflow_id):
    return int(workflow_id.rsplit("-", 1)[1])


class ImportRepository:
    def __init__(self, pool, namespace=None):
        self.pool = pool
        self.namespace = namespace or get_settings().temporal_namespace

    def create(self, program_id, fingerprint, key, stored):
        used = set()
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute(
                "SELECT current_version_id FROM programs WHERE id=%s FOR UPDATE", (program_id,)
            )
            program = Repository.require(cur.fetchone())
            if (
                cur.execute(
                    "SELECT id FROM program_versions WHERE program_id=%s AND operation_key=%s",
                    (program_id, key),
                ).fetchone()
                or cur.execute(
                    "SELECT id FROM processing_jobs WHERE program_id=%s "
                    "AND type<>'import' AND operation_key=%s",
                    (program_id, key),
                ).fetchone()
            ):
                raise DomainError(409, "Ключ уже использован для другой операции")
            previous = cur.execute(
                """SELECT id,request_hash FROM processing_jobs
                   WHERE program_id=%s AND type='import'
                   AND (operation_key=%s OR parameters->'request_keys' ? %s)""",
                (program_id, key, key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise DomainError(409, "Ключ операции уже использован для другого пакета")
                return previous["id"], used
            previous = cur.execute(
                """SELECT id FROM processing_jobs WHERE program_id=%s AND type='import'
                   AND parameters->>'fingerprint'=%s""",
                (program_id, fingerprint),
            ).fetchone()
            if previous:
                cur.execute(
                    """UPDATE processing_jobs SET parameters=jsonb_set(
                       parameters,'{request_keys}',parameters->'request_keys'||to_jsonb(%s::text))
                       WHERE id=%s""",
                    (key, previous["id"]),
                )
                return previous["id"], used
            job_id = uuid4()
            documents = []
            for member_name, item, parse in stored:
                previous = cur.execute(
                    """SELECT id FROM source_documents WHERE program_id=%s AND sha256=%s
                       ORDER BY created_at,id LIMIT 1""",
                    (program_id, item["sha256"]),
                ).fetchone()
                if previous:
                    document_id = previous["id"]
                else:
                    document_id = item["id"]
                    Repository.insert_document(cur, program_id, item)
                    used.add(document_id)
                if parse:
                    documents.append({"document_id": str(document_id), "member_name": member_name})
            cur.execute(
                """INSERT INTO processing_jobs (
                   id,program_id,input_version_id,operation_key,request_hash,type,
                   temporal_namespace,workflow_id,parameters)
                   VALUES (%s,%s,%s,%s,%s,'import',%s,%s,%s)""",
                (
                    job_id,
                    program_id,
                    program["current_version_id"],
                    key,
                    fingerprint,
                    self.namespace,
                    workflow_identity(job_id, 1),
                    Jsonb(
                        {"fingerprint": fingerprint, "request_keys": [key], "documents": documents}
                    ),
                ),
            )
            return job_id, used

    def read(self, program_id, job_id, *, include_output=False):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            row = Repository.require(
                cur.execute(
                    "SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s AND type='import'",
                    (program_id, job_id),
                ).fetchone()
            )
            parameters, report = row["parameters"], row["report"]
            if include_output:
                report = hydrate_report(cur, report)
                if row["output_graph_state_id"]:
                    report["snapshot"] = load_graph(cur, row["output_graph_state_id"])

            ids = [UUID(d["document_id"]) for d in parameters["documents"]]
            originals = {
                str(d["id"]): d
                for d in cur.execute(
                    "SELECT id,sha256 FROM source_documents WHERE program_id=%s AND id=ANY(%s)",
                    (program_id, ids),
                ).fetchall()
            }
        files = []
        for member in parameters["documents"]:
            identity = member["document_id"]
            result = report.get("files", {}).get(identity, {})
            original = originals[identity]
            item = {
                "document_id": identity,
                "member_name": member["member_name"],
                "sha256": original["sha256"],
                "status": "failed"
                if result.get("error_code")
                else (
                    "completed" if result.get("output") or result.get("artifact_id") else "pending"
                ),
                "error_code": result.get("error_code"),
                "error_message": result.get("error_message"),
            }
            if include_output:
                item.update(output=result.get("output"))
            files.append(item)
        outcome = report.get("outcome")
        updated_at = report.get("completed_at")
        job = {
            "id": row["id"],
            "program_id": row["program_id"],
            "attempt": workflow_attempt(row["workflow_id"]),
            "status": outcome or "pending",
            "progress": (95 if outcome == "failed" else 100) if outcome else 0,
            "issues": report.get("issues", []),
            "files": files,
            "created_at": row["created_at"],
            "updated_at": datetime.fromisoformat(updated_at) if updated_at else row["created_at"],
            "_workflow_id": row["workflow_id"],
            "_namespace": row["temporal_namespace"],
            "_terminal": outcome is not None,
            "_legacy": report.get("legacy_import", False),
        }
        if include_output:
            job["snapshot"] = report.get("snapshot")
        return job

    def job(self, job_id, *, include_output=False):
        with self.pool.connection() as connection:
            row = Repository.require(
                connection.execute(
                    "SELECT program_id FROM processing_jobs WHERE id=%s AND type='import'",
                    (job_id,),
                ).fetchone()
            )
        return self.read(row[0], job_id, include_output=include_output)

    def pending(self):
        with self.pool.connection() as connection:
            return connection.execute(
                """SELECT id,workflow_id,report->>'legacy_import'='true'
                   FROM processing_jobs WHERE type='import' AND NOT report ? 'outcome'
                   ORDER BY created_at,id"""
            ).fetchall()

    def file_finished(self, job_id, document_id, attempt, output=None, code=None, message=None):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT program_id FROM processing_jobs WHERE id=%s", (job_id,)
            ).fetchone()
            if row is None:
                return
            cur.execute("SELECT id FROM programs WHERE id=%s FOR UPDATE", (row["program_id"],))
            job = cur.execute(
                "SELECT * FROM processing_jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if (
                job["workflow_id"] != workflow_identity(job_id, attempt)
                or "outcome" in job["report"]
            ):
                return
            report = job["report"]
            previous = report.get("files", {}).get(str(document_id))
            if previous and previous.get("artifact_id"):
                return
            artifact = (
                parsed_artifact(cur, job["program_id"], document_id, output, job_id, attempt)
                if output
                else None
            )
            result = {
                "artifact_id": str(artifact) if artifact else None,
                "error_code": code,
                "error_message": message,
            }
            report.setdefault("files", {})[str(document_id)] = result
            cur.execute("UPDATE processing_jobs SET report=%s WHERE id=%s", (Jsonb(report), job_id))

    def finish(self, job_id, attempt, status, snapshot, issues):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT program_id FROM processing_jobs WHERE id=%s", (job_id,)
            ).fetchone()
            if row is None:
                return
            cur.execute("SELECT id FROM programs WHERE id=%s FOR UPDATE", (row["program_id"],))
            job = cur.execute(
                "SELECT * FROM processing_jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if (
                job["workflow_id"] != workflow_identity(job_id, attempt)
                or "outcome" in job["report"]
            ):
                return
            graph = (
                save_graph(
                    cur,
                    job["program_id"],
                    snapshot,
                    producer=job_id,
                    attempt=attempt,
                    members={
                        d["document_id"]: d["member_name"] for d in job["parameters"]["documents"]
                    },
                )
                if snapshot
                else None
            )
            report = job["report"]
            report.update(
                outcome=status, issues=issues, completed_at=datetime.now().astimezone().isoformat()
            )
            cur.execute(
                "UPDATE processing_jobs SET report=%s,output_graph_state_id=%s WHERE id=%s",
                (Jsonb(report), graph, job_id),
            )

    def retry(self, program_id, job_id, expected_workflow_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id FROM programs WHERE id=%s FOR UPDATE", (program_id,))
            row = Repository.require(
                cur.execute(
                    """SELECT workflow_id,report FROM processing_jobs
                   WHERE id=%s AND program_id=%s AND type='import' FOR UPDATE""",
                    (job_id, program_id),
                ).fetchone()
            )
            if row["workflow_id"] != expected_workflow_id:
                return
            # Сохраняем успешные наблюдения; новый workflow разбирает только оставшиеся документы.
            successful = {
                identity: result
                for identity, result in row["report"].get("files", {}).items()
                if (result.get("output") or result.get("artifact_id"))
                and not result.get("error_code")
            }
            cur.execute(
                "UPDATE processing_jobs SET "
                "workflow_id=%s,report=%s,output_graph_state_id=NULL WHERE id=%s",
                (
                    workflow_identity(job_id, workflow_attempt(expected_workflow_id) + 1),
                    Jsonb({"files": successful}),
                    job_id,
                ),
            )
