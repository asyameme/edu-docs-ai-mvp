"""Бизнес-задачи построения графа без копирования состояния Temporal в SQL."""

from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.errors import DomainError
from app.graph_projection import CONVERTER_REVISION, canonical, project_snapshot
from app.repository import Repository
from app.review_logic import digest
from app.review_repository import ReviewRepository
from app.review_storage import key_in_jobs


class GraphRepository:
    def __init__(self, pool):
        self.pool = pool
        self.settings = get_settings()
        self.cursor = None

    def projection(self, program_id, version_id):
        row = Repository(self.pool).version(program_id, version_id)
        snapshot = row["snapshot"]
        if (
            len(snapshot.get("entities", [])) + len(snapshot.get("relations", []))
            > self.settings.graph_max_objects
            or len(canonical(snapshot).encode()) > self.settings.graph_max_snapshot_bytes
        ):
            raise DomainError(413, "Снимок превышает настроенный предел атомарной сборки графа")
        return project_snapshot(program_id, version_id, snapshot)

    def create(self, program_id, version_id, key, *, automatic=False):
        graph = self.projection(program_id, version_id)
        fingerprint = digest(
            {
                "operation": "build_graph",
                "version_id": str(version_id),
                "snapshot_hash": graph["snapshot_hash"],
                "revision": CONVERTER_REVISION,
            }
        )
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            ReviewRepository.lock(cur, program_id)
            row = cur.execute(
                """SELECT * FROM processing_jobs WHERE program_id=%s
                AND operation_key=%s""",
                (program_id, key),
            ).fetchone()
            if row:
                if row["type"] != "build_graph" or row["request_hash"] != fingerprint:
                    raise DomainError(409, "Ключ использован для другого запроса")
                return row["id"]
            if (
                key_in_jobs(cur, program_id, key)
                or cur.execute(
                    "SELECT id FROM program_versions WHERE program_id=%s AND operation_key=%s",
                    (program_id, key),
                ).fetchone()
            ):
                raise DomainError(409, "Ключ использован для другой операции")
            if automatic:
                active = cur.execute(
                    """SELECT id FROM processing_jobs WHERE program_id=%s
                    AND input_version_id=%s AND type='build_graph' AND NOT report ? 'outcome'
                    ORDER BY created_at LIMIT 1""",
                    (program_id, version_id),
                ).fetchone()
                if active:
                    return active["id"]
            job_id = uuid4()
            parameters = {
                "snapshot_hash": graph["snapshot_hash"],
                "converter_revision": CONVERTER_REVISION,
                "automatic": automatic,
            }
            cur.execute(
                """INSERT INTO processing_jobs(id,program_id,input_version_id,
                operation_key,request_hash,type,temporal_namespace,workflow_id,parameters)
                VALUES(%s,%s,%s,%s,%s,'build_graph',%s,%s,%s)""",
                (
                    job_id,
                    program_id,
                    version_id,
                    key,
                    fingerprint,
                    self.settings.temporal_namespace,
                    f"graph-{job_id}",
                    Jsonb(parameters),
                ),
            )
            return job_id

    def job(self, program_id, job_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            return Repository.require(
                cur.execute(
                    """SELECT * FROM processing_jobs
                WHERE program_id=%s AND id=%s AND type='build_graph'""",
                    (program_id, job_id),
                ).fetchone()
            )

    def job_by_id(self, job_id):
        with self.pool.connection() as connection:
            row = Repository.require(
                connection.execute(
                    "SELECT program_id FROM processing_jobs WHERE id=%s AND type='build_graph'",
                    (job_id,),
                ).fetchone()
            )
        return self.job(row[0], job_id)

    def pending(self):
        with self.pool.connection() as connection:
            return connection.execute(
                """SELECT id,workflow_id FROM processing_jobs
                WHERE type='build_graph' AND temporal_namespace=%s
                AND NOT report ? 'outcome' ORDER BY created_at,id""",
                (self.settings.temporal_namespace,),
            ).fetchall()

    def recover_completed(self, job_id, store):
        # SQL-backup мог предшествовать завершению сохранённого Workflow в Temporal.
        job = self.job_by_id(job_id)
        graph = self.projection(job["program_id"], job["input_version_id"])
        if (
            graph["snapshot_hash"] != job["parameters"]["snapshot_hash"]
            or graph["converter_revision"] != job["parameters"]["converter_revision"]
        ):
            self.fail(job_id, "graph_invalid_snapshot")
            return
        try:
            store.read(graph)
        except DomainError:
            self.fail(job_id, "graph_rebuild_required")
        else:
            self.finish(job, graph)

    def finish(self, job, graph):
        manifest = {key: value for key, value in graph.items() if key not in {"nodes", "edges"}}
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE processing_jobs SET output_version_id=input_version_id,report=%s
                WHERE id=%s AND workflow_id=%s AND NOT report ? 'outcome'""",
                (
                    Jsonb({"outcome": "completed", "manifest": manifest}),
                    job["id"],
                    job["workflow_id"],
                ),
            )

    def fail(self, job_id, code="graph_unavailable"):
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE processing_jobs SET report=%s WHERE id=%s
                AND type='build_graph' AND NOT report ? 'outcome'""",
                (
                    Jsonb(
                        {
                            "outcome": "failed",
                            "code": code,
                            "message": "Граф не построен; SQL-версия сохранена. "
                            "Можно повторить сборку",
                        }
                    ),
                    job_id,
                ),
            )

    def reconcile(self, store):
        if not self.settings.graph_auto_build:
            return
        store.ping()
        with self.pool.connection() as connection:
            rows = connection.execute(
                """SELECT program_id,id FROM program_versions
                WHERE (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT 20""",
                (self.cursor, self.cursor),
            ).fetchall()
        if not rows:
            self.cursor = None
            return
        for program_id, version_id in rows:
            self.cursor = version_id
            try:
                graph = self.projection(program_id, version_id)
                store.read(graph)
            except DomainError as error:
                if error.status not in {409, 503}:
                    continue
                self.create(program_id, version_id, "graph:auto:" + uuid4().hex, automatic=True)
