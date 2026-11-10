"""Задачи генерации и неизменяемые ZIP для выбранной версии программы."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.domain import SnapshotV2
from app.errors import DomainError
from app.repository import Repository


def _source_files(imported, snapshot):
    members = {
        item["document_id"]: item["member_name"]
        for item in imported["parameters"]["documents"]
    }
    parsed = imported["report"].get("files", {})
    sources = []
    for document_id in snapshot.document_ids:
        identity = str(document_id)
        output = parsed.get(identity, {}).get("output")
        if identity not in members or not output:
            raise DomainError(409, "Не все документы версии разобраны")
        meta = output.get("meta", {})
        if not meta.get("doc_type") or not meta.get("file_hash"):
            raise DomainError(409, "У результата разбора нет типа или хэша документа")
        sources.append({
            "document_id": identity,
            "member_name": members[identity],
            "doc_type": meta["doc_type"],
            "file_hash": meta["file_hash"],
        })
    return sources


class GenerationRepository:
    def __init__(self, pool):
        self.pool = pool

    def create(self, program_id, version_id, import_job_id, key):
        fingerprint = hashlib.sha256(
            json.dumps(
                {"version_id": str(version_id) if version_id else None,
                 "import_job_id": str(import_job_id) if import_job_id else None},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL statement_timeout='5s'")
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
                row = Repository.require(
                    cur.execute(
                        "SELECT snapshot FROM program_versions WHERE program_id=%s AND id=%s",
                        (program_id, version_id),
                    ).fetchone()
                )
                snapshot = SnapshotV2.model_validate(row["snapshot"])
                if not snapshot.review or not snapshot.review.confirmation:
                    raise DomainError(409, "Для генерации версии нужно подтверждение")
                input_version_id = version_id
                source_import_id = snapshot.review.import_job_id
            else:
                row = Repository.require(
                    cur.execute(
                        "SELECT input_version_id,parameters,report FROM processing_jobs "
                        "WHERE program_id=%s AND id=%s AND type='import'",
                        (program_id, import_job_id),
                    ).fetchone()
                )
                if row["report"].get("outcome") not in {"completed", "partial"}:
                    raise DomainError(409, "Импорт ещё не завершён")
                if any(
                    not row["report"].get("files", {}).get(item["document_id"], {}).get("output")
                    for item in row["parameters"]["documents"]
                ):
                    raise DomainError(409, "Не все документы архива разобраны")
                if not row["report"].get("snapshot"):
                    raise DomainError(409, "У импорта нет результата разбора")
                snapshot = SnapshotV2.model_validate(row["report"]["snapshot"])
                if set(snapshot.document_ids) != {
                    UUID(item["document_id"])
                    for item in row["parameters"]["documents"]
                }:
                    raise DomainError(409, "Результат импорта не совпадает с документами")
                input_version_id = row["input_version_id"]
                source_import_id = import_job_id
            if not snapshot.document_ids:
                raise DomainError(409, "Нет исходных документов для генерации")
            if version_id is not None:
                row = Repository.require(
                    cur.execute(
                        "SELECT parameters,report FROM processing_jobs "
                        "WHERE program_id=%s AND id=%s AND type='import'",
                        (program_id, source_import_id),
                    ).fetchone()
                )
            parameters = {
                "version_id": str(version_id) if version_id else None,
                "import_job_id": str(import_job_id) if import_job_id else None,
                "documents": _source_files(row, snapshot),
            }
            if import_job_id is not None:
                parameters["snapshot"] = snapshot.model_dump(mode="json")
            job_id = uuid4()
            cur.execute(
                """INSERT INTO processing_jobs(
                   id,program_id,input_version_id,operation_key,request_hash,type,
                   temporal_namespace,workflow_id,parameters)
                   VALUES(%s,%s,%s,%s,%s,'generate',%s,%s,%s)""",
                (
                    job_id,
                    program_id,
                    input_version_id,
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
                    "SELECT id,program_id,parameters->>'version_id' AS version_id,"
                    "parameters->>'import_job_id' AS import_job_id,report,created_at "
                    "FROM processing_jobs "
                    "WHERE program_id=%s AND id=%s AND type='generate'",
                    (program_id, job_id),
                ).fetchone()
            )
            report = row["report"]
            return {
                "id": row["id"],
                "program_id": row["program_id"],
                "version_id": row["version_id"],
                "import_job_id": row["import_job_id"],
                "status": report.get("outcome")
                or ("processing" if report.get("started_at") else "pending"),
                "error": report.get("error"),
                "size_bytes": report.get("size_bytes"),
                "sha256": report.get("sha256"),
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
            job = Repository.require(
                cur.execute(
                    "SELECT * FROM processing_jobs WHERE id=%s AND type='generate'", (job_id,)
                ).fetchone()
            )
            if job["report"].get("outcome"):
                return job["report"]["outcome"]
            cur.execute(
                """UPDATE processing_jobs SET report=report||%s::jsonb
                   WHERE id=%s AND NOT report ? 'outcome'""",
                (Jsonb({"started_at": datetime.now(UTC).isoformat()}), job_id),
            )
            if job["parameters"].get("import_job_id"):
                snapshot = SnapshotV2.model_validate(job["parameters"]["snapshot"])
            else:
                version = Repository.require(
                    cur.execute(
                        "SELECT snapshot FROM program_versions WHERE program_id=%s AND id=%s",
                        (job["program_id"], job["input_version_id"]),
                    ).fetchone()
                )
                snapshot = SnapshotV2.model_validate(version["snapshot"])
            originals = cur.execute(
                """SELECT id,original_name,format,content,sha256 FROM source_documents
                   WHERE program_id=%s AND id=ANY(%s)""",
                (job["program_id"], snapshot.document_ids),
            ).fetchall()
            if {row["id"] for row in originals} != set(snapshot.document_ids):
                raise DomainError(409, "Не все исходные документы версии доступны")
            return snapshot, job["parameters"]["documents"], originals

    def finish(self, job_id, content):
        checksum = hashlib.sha256(content).hexdigest()
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            job = Repository.require(
                cur.execute(
                    "SELECT program_id,parameters->>'version_id' AS version_id,"
                    "parameters->>'import_job_id' AS import_job_id,report FROM processing_jobs "
                    "WHERE id=%s FOR UPDATE",
                    (job_id,),
                ).fetchone()
            )
            if "outcome" in job["report"]:
                return job["report"]["outcome"]
            cur.execute(
                """INSERT INTO generated_packages(
                   job_id,program_id,version_id,import_job_id,content,size_bytes,sha256)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    job_id,
                    job["program_id"],
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
                        }
                    ),
                    job_id,
                ),
            )
            return "completed"

    def fail(self, job_id, message):
        with self.pool.connection() as connection:
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
