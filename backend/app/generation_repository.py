"""Задачи генерации и неизменяемые ZIP для выбранной версии программы."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.domain import SnapshotV2
from app.errors import DomainError
from app.graph_storage import load_graph
from app.repository import Repository, limit_lock_wait


def _source_files(cur, graph_id):
    rows = cur.execute(
        "SELECT g.document_id,g.member_name,a.parsed_output FROM graph_documents g "
        "LEFT JOIN document_artifacts a ON a.id=g.parsed_artifact_id "
        "WHERE g.graph_state_id=%s ORDER BY g.position",
        (graph_id,),
    ).fetchall()
    sources = []
    for row in rows:
        meta = (row["parsed_output"] or {}).get("meta", {})
        if not meta.get("doc_type") or not meta.get("file_hash"):
            raise DomainError(409, "Не все документы выбранного графа разобраны")
        sources.append(
            {
                "document_id": str(row["document_id"]),
                "member_name": row["member_name"],
                "doc_type": meta["doc_type"],
                "file_hash": meta["file_hash"],
            }
        )
    return sources


class GenerationRepository:
    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def _checked_snapshot(cur, program_id, version_id, graph_state_id, review):
        if graph_state_id is None or (review or {}).get("graph_ready") is False:
            raise DomainError(409, "Сначала соберите граф из выбранных значений документов")
        snapshot = SnapshotV2.model_validate(load_graph(cur, graph_state_id, review))
        from app.version_policy import require_generation_check

        checked = cur.execute(
            "SELECT id,program_id,input_version_id,parameters,report FROM processing_jobs "
            "WHERE program_id=%s AND input_version_id=%s AND type='validate' "
            "AND parameters->>'purpose'='check' "
            "AND parameters->>'manual_rule_codes' IS NULL "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (program_id, version_id),
        ).fetchone()
        require_generation_check(snapshot, program_id, version_id, checked)
        return snapshot

    def create(self, program_id, version_id, import_job_id, key):
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "version_id": str(version_id) if version_id else None,
                    "import_job_id": str(import_job_id) if import_job_id else None,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            # Чтение проверенного графа после блокировки не лимитируется 5 с.
            limit_lock_wait(cur)
            Repository.require(
                cur.execute(
                    "SELECT id FROM programs WHERE id=%s FOR UPDATE", (program_id,)
                ).fetchone()
            )
            if cur.execute(
                "SELECT id FROM program_versions WHERE program_id=%s AND operation_key=%s",
                (program_id, key),
            ).fetchone():
                raise DomainError(409, "Ключ уже использован для сохранения версии")
            previous = cur.execute(
                "SELECT id,type,request_hash FROM processing_jobs "
                "WHERE program_id=%s AND operation_key=%s",
                (program_id, key),
            ).fetchone()
            if previous:
                if previous["type"] != "generate" or previous["request_hash"] != fingerprint:
                    raise DomainError(409, "Ключ уже использован для другой операции")
                return previous["id"]
            if cur.execute(
                """SELECT id FROM processing_jobs WHERE program_id=%s
                   AND type='import' AND parameters->'request_keys' ? %s""",
                (program_id, key),
            ).fetchone():
                raise DomainError(409, "Ключ уже использован для импорта")
            if version_id is not None:
                version = Repository.require(
                    cur.execute(
                        "SELECT graph_state_id,review FROM program_versions WHERE "
                        "program_id=%s AND id=%s",
                        (program_id, version_id),
                    ).fetchone()
                )
                graph_id = version["graph_state_id"]
                snapshot = self._checked_snapshot(
                    cur, program_id, version_id, graph_id, version["review"]
                )
                input_version_id = version_id
            else:
                raise DomainError(
                    409, "Генерация разрешена только для проверенной сохранённой версии"
                )
            if not snapshot.document_ids:
                raise DomainError(409, "Нет исходных документов для генерации")
            parameters = {
                "version_id": str(version_id) if version_id else None,
                "import_job_id": str(import_job_id) if import_job_id else None,
                "documents": _source_files(cur, graph_id),
            }
            job_id = uuid4()
            cur.execute(
                "INSERT INTO processing_jobs(\n                   id,program_id,"
                "input_version_id,input_graph_state_id,operation_key,request_hash,type,\n "
                "                  temporal_namespace,workflow_id,parameters)\n           "
                "        VALUES(%s,%s,%s,%s,%s,%s,'generate',%s,%s,%s)",
                (
                    job_id,
                    program_id,
                    input_version_id,
                    graph_id,
                    key,
                    fingerprint,
                    get_settings().temporal_namespace,
                    f"generation-{job_id}",
                    Jsonb(parameters),
                ),
            )
            return job_id

    def job(self, program_id, job_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            row = Repository.require(
                cur.execute(
                    "SELECT j.id,j.program_id,j.parameters->>'version_id' AS version_id,"
                    "j.parameters->>'import_job_id' AS import_job_id,j.report,j.created_at,"
                    "j.workflow_id,j.temporal_namespace,v.number AS version_number "
                    "FROM processing_jobs j LEFT JOIN program_versions v "
                    "ON v.program_id=j.program_id AND v.id::text=j.parameters->>'version_id' "
                    "WHERE j.program_id=%s AND j.id=%s AND j.type='generate'",
                    (program_id, job_id),
                ).fetchone()
            )
            report = row["report"]
            return {
                "id": row["id"],
                "program_id": row["program_id"],
                "version_id": row["version_id"],
                "version_number": row["version_number"],
                "import_job_id": row["import_job_id"],
                "status": report.get("outcome") or "pending",
                "_workflow_id": row["workflow_id"],
                "_namespace": row["temporal_namespace"],
                "error": report.get("error"),
                "size_bytes": report.get("size_bytes"),
                "sha256": report.get("sha256"),
                "applied": report.get("applied") or [],
                "notes": report.get("notes") or [],
                "created_at": row["created_at"],
            }

    def pending(self):
        with self.pool.connection() as connection:
            return connection.execute(
                """SELECT id,workflow_id FROM processing_jobs WHERE type='generate'
                   AND NOT report ? 'outcome' ORDER BY created_at,id"""
            ).fetchall()

    def input(self, job_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT p.id FROM programs p JOIN processing_jobs j ON j.program_id=p.id "
                "WHERE j.id=%s FOR UPDATE OF p",
                (job_id,),
            )
            job = Repository.require(
                cur.execute(
                    "SELECT * FROM processing_jobs WHERE id=%s AND type='generate'", (job_id,)
                ).fetchone()
            )
            if job["report"].get("outcome"):
                return job["report"]["outcome"]
            review = None
            if job["parameters"].get("version_id"):
                review = Repository.require(
                    cur.execute(
                        "SELECT review FROM program_versions WHERE id=%s",
                        (job["input_version_id"],),
                    ).fetchone()
                )["review"]
            snapshot = SnapshotV2.model_validate(
                load_graph(cur, job["input_graph_state_id"], review)
            )
            self._checked_snapshot(
                cur,
                job["program_id"],
                job["input_version_id"],
                job["input_graph_state_id"],
                review,
            )
            originals = cur.execute(
                """SELECT id,original_name,format,content,sha256 FROM source_documents
                   WHERE program_id=%s AND id=ANY(%s)""",
                (job["program_id"], snapshot.document_ids),
            ).fetchall()
            if {row["id"] for row in originals} != set(snapshot.document_ids):
                raise DomainError(409, "Не все исходные документы версии доступны")
            return snapshot, job["parameters"]["documents"], originals, self._parsed_documents(
                cur, job["program_id"], snapshot
            )

    @staticmethod
    def _parsed_documents(cur, program_id, snapshot):
        """Разбор импорта до решений пользователя: по нему решения находят строки документов."""
        if snapshot.review is None:
            return {}
        from app.graph_storage import hydrate_job

        review = hydrate_job(
            cur,
            Repository.require(
                cur.execute(
                    "SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s "
                    "AND type='validate'",
                    (program_id, snapshot.review.review_id),
                ).fetchone()
            ),
        )
        return {
            str(item["document_id"]): item["parsed"]
            for item in review["parameters"]["input"]["documents"]
        }

    def composition(self, program_id):
        """Документы, которые войдут в ZIP: тот же состав, из которого работает генератор."""
        Repository(self.pool).program(program_id)
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            version = cur.execute(
                "SELECT graph_state_id,review FROM program_versions v JOIN programs p "
                "ON p.current_version_id=v.id WHERE p.id=%s",
                (program_id,),
            ).fetchone()
            pending = (version["review"] or {}).get("graph_ready") is False
            if version["graph_state_id"] is None or pending:
                return {"ready": False, "documents": []}
            try:
                return {"ready": True, "documents": _source_files(cur, version["graph_state_id"])}
            except DomainError:
                return {"ready": False, "documents": []}

    def listing(self, program_id, limit, offset):
        Repository(self.pool).program(program_id)
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """SELECT id FROM processing_jobs WHERE program_id=%s AND type='generate'
                   ORDER BY created_at DESC,id LIMIT %s OFFSET %s""",
                (program_id, limit, offset),
            ).fetchall()
        return [self.job(program_id, row["id"]) for row in rows]

    def finish(self, job_id, content, notes=(), applied=()):
        checksum = hashlib.sha256(content).hexdigest()
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT p.id FROM programs p JOIN processing_jobs j ON j.program_id=p.id "
                "WHERE j.id=%s FOR UPDATE OF p",
                (job_id,),
            )
            job = Repository.require(
                cur.execute(
                    "SELECT program_id,input_graph_state_id,parameters->>'version_id' AS "
                    "version_id,"
                    "parameters->>'import_job_id' AS import_job_id,report FROM processing_jobs "
                    "WHERE id=%s FOR UPDATE",
                    (job_id,),
                ).fetchone()
            )
            if "outcome" in job["report"]:
                return job["report"]["outcome"]
            cur.execute(
                "INSERT INTO generated_packages(\n                   job_id,program_id,"
                "graph_state_id,version_id,import_job_id,content,size_bytes,sha256)\n     "
                "              VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    job_id,
                    job["program_id"],
                    job["input_graph_state_id"],
                    job["version_id"],
                    job["import_job_id"],
                    content,
                    len(content),
                    checksum,
                ),
            )
            cur.execute(
                """UPDATE processing_jobs SET report=report||%s::jsonb WHERE id=%s""",
                (
                    Jsonb(
                        {
                            "outcome": "completed",
                            "size_bytes": len(content),
                            "sha256": checksum,
                            "completed_at": datetime.now(UTC).isoformat(),
                            # Что изменено в документах и что оставлено для ручной правки.
                            "applied": list(applied),
                            "notes": list(notes),
                        }
                    ),
                    job_id,
                ),
            )
            return "completed"

    def fail(self, job_id, message):
        with self.pool.connection() as connection:
            connection.execute(
                "SELECT p.id FROM programs p JOIN processing_jobs j ON "
                "j.program_id=p.id "
                "WHERE j.id=%s FOR UPDATE OF p",
                (job_id,),
            )
            connection.execute(
                """UPDATE processing_jobs SET report=report||%s::jsonb
                   WHERE id=%s AND NOT report ? 'outcome'""",
                (
                    Jsonb(
                        {
                            "outcome": "failed",
                            "error": message,
                            "completed_at": datetime.now(UTC).isoformat(),
                        }
                    ),
                    job_id,
                ),
            )

    def package(self, program_id, job_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            job = Repository.require(
                cur.execute(
                    "SELECT report FROM processing_jobs "
                    "WHERE program_id=%s AND id=%s AND type='generate'",
                    (program_id, job_id),
                ).fetchone()
            )
            if job["report"].get("outcome") != "completed":
                raise DomainError(409, "ZIP ещё не готов")
            return Repository.require(
                cur.execute(
                    "SELECT content,size_bytes,sha256 FROM generated_packages "
                    "WHERE program_id=%s AND job_id=%s",
                    (program_id, job_id),
                ).fetchone()
            )
