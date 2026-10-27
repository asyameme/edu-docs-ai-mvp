"""Задача одновременно является записью доставки в Temporal; результаты не теряются при retry."""

from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.errors import DomainError
from app.repository import Repository

JOB_COLUMNS = "id, program_id, attempt, status, progress, issues, created_at, updated_at"


class ImportRepository:
    def __init__(self, pool):
        self.pool = pool

    def create(self, program_id, fingerprint, key, stored):
        used = set()
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute("SELECT id FROM programs WHERE id=%s FOR UPDATE", (program_id,))
            Repository.require(cur.fetchone())
            cur.execute(
                """SELECT j.id, j.fingerprint FROM import_requests r
                   JOIN import_jobs j ON j.id=r.job_id
                   WHERE r.program_id=%s AND r.operation_key=%s""",
                (program_id, key),
            )
            previous = cur.fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise DomainError(409, "Ключ операции уже использован для другого пакета")
                return previous["id"], used
            cur.execute(
                "SELECT id FROM import_jobs WHERE program_id=%s AND fingerprint=%s",
                (program_id, fingerprint),
            )
            previous = cur.fetchone()
            if previous:
                cur.execute(
                    "INSERT INTO import_requests(program_id,operation_key,job_id) "
                    "VALUES (%s,%s,%s)",
                    (program_id, key, previous["id"]),
                )
                return previous["id"], used
            job_id = uuid4()
            cur.execute(
                "INSERT INTO import_jobs(id,program_id,fingerprint) VALUES (%s,%s,%s)",
                (job_id, program_id, fingerprint),
            )
            cur.execute(
                "INSERT INTO import_requests(program_id,operation_key,job_id) VALUES (%s,%s,%s)",
                (program_id, key, job_id),
            )
            for member_name, item in stored:
                cur.execute(
                    """SELECT id FROM source_documents WHERE program_id=%s AND sha256=%s
                       ORDER BY created_at,id LIMIT 1""",
                    (program_id, item["sha256"]),
                )
                previous = cur.fetchone()
                if previous:
                    document_id = previous["id"]
                else:
                    document_id = item["id"]
                    cur.execute(
                        """INSERT INTO source_documents
                           (id,program_id,original_name,format,media_type,size_bytes,sha256,storage_key)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            document_id,
                            program_id,
                            item["original_name"],
                            item["format"],
                            item["media_type"],
                            item["size_bytes"],
                            item["sha256"],
                            item["storage_key"],
                        ),
                    )
                    used.add(document_id)
                cur.execute(
                    """INSERT INTO import_files(job_id,program_id,document_id,member_name)
                       VALUES (%s,%s,%s,%s)""",
                    (job_id, program_id, document_id, member_name),
                )
            return job_id, used

    def read(self, program_id, job_id, *, include_output=False):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT "
                + JOB_COLUMNS
                + (", snapshot" if include_output else "")
                + " FROM import_jobs WHERE program_id=%s AND id=%s",
                (program_id, job_id),
            )
            job = Repository.require(cur.fetchone())
            cur.execute(
                "SELECT f.document_id, f.member_name, f.status, f.error_code, f.error_message, "
                "d.sha256"
                + (", d.storage_key, f.output" if include_output else "")
                + " FROM import_files f JOIN source_documents d ON d.id=f.document_id "
                "WHERE f.job_id=%s ORDER BY f.member_name,f.document_id",
                (job_id,),
            )
            job["files"] = cur.fetchall()
        return job

    def pending(self):
        with self.pool.connection() as connection:
            return connection.execute(
                """SELECT id,attempt FROM import_jobs
                   WHERE NOT workflow_started AND status='pending'
                   ORDER BY created_at,id LIMIT 100"""
            ).fetchall()

    def dispatched(self, job_id, attempt):
        with self.pool.connection() as connection:
            connection.execute(
                "UPDATE import_jobs SET workflow_started=true WHERE id=%s AND attempt=%s",
                (job_id, attempt),
            )

    def begin(self, job_id, attempt):
        with self.pool.connection() as connection:
            row = connection.execute(
                """UPDATE import_jobs SET status='processing',progress=greatest(progress,1),
                   updated_at=now() WHERE id=%s AND attempt=%s
                   AND status IN ('pending','processing') RETURNING program_id""",
                (job_id, attempt),
            ).fetchone()
            return str(row[0]) if row else None

    def file_started(self, job_id, document_id, attempt):
        with self.pool.connection() as connection:
            if not self.lock_attempt(connection, job_id, attempt):
                return
            connection.execute(
                """UPDATE import_files SET status='processing'
                   WHERE job_id=%s AND document_id=%s AND status='pending'
                   AND EXISTS (SELECT 1 FROM import_jobs WHERE id=%s AND attempt=%s)""",
                (job_id, document_id, job_id, attempt),
            )

    def file_finished(self, job_id, document_id, attempt, output=None, code=None, message=None):
        with self.pool.connection() as connection:
            if not self.lock_attempt(connection, job_id, attempt):
                return
            connection.execute(
                """UPDATE import_files SET status=%s,output=%s,error_code=%s,error_message=%s
                   WHERE job_id=%s AND document_id=%s AND status NOT IN ('completed','failed')
                   AND EXISTS (SELECT 1 FROM import_jobs WHERE id=%s AND attempt=%s)""",
                (
                    "failed" if code else "completed",
                    Jsonb(output) if output else None,
                    code,
                    message,
                    job_id,
                    document_id,
                    job_id,
                    attempt,
                ),
            )
            connection.execute(
                """UPDATE import_jobs SET progress=greatest(progress,
                   (SELECT 5+90*count(*) FILTER (WHERE status IN ('completed','failed'))::integer
                    /greatest(count(*)::integer,1) FROM import_files WHERE job_id=%s)),
                   updated_at=now() WHERE id=%s AND attempt=%s AND status='processing'""",
                (job_id, job_id, attempt),
            )

    def finish(self, job_id, attempt, status, snapshot, issues):
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE import_jobs SET status=%s,progress=%s,snapshot=%s,
                   issues=%s,updated_at=now() WHERE id=%s AND attempt=%s AND status='processing'""",
                (
                    status,
                    95 if status == "failed" else 100,
                    Jsonb(snapshot) if snapshot else None,
                    Jsonb(issues),
                    job_id,
                    attempt,
                ),
            )

    def retry(self, program_id, job_id):
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT status FROM import_jobs WHERE id=%s AND program_id=%s FOR UPDATE",
                (job_id, program_id),
            ).fetchone()
            Repository.require(row)
            if row[0] not in {"partial", "failed"}:
                return
            connection.execute(
                """UPDATE import_jobs SET status='pending',progress=0,attempt=attempt+1,
                   workflow_started=false,snapshot=NULL,issues='[]'::jsonb,updated_at=now()
                   WHERE id=%s""",
                (job_id,),
            )
            connection.execute(
                """UPDATE import_files SET status='pending',error_code=NULL,error_message=NULL
                   WHERE job_id=%s AND status='failed'""",
                (job_id,),
            )

    @staticmethod
    def lock_attempt(connection, job_id, attempt):
        row = connection.execute(
            "SELECT attempt,status FROM import_jobs WHERE id=%s FOR UPDATE",
            (job_id,),
        ).fetchone()
        return row is not None and row[0] == attempt and row[1] == "processing"
