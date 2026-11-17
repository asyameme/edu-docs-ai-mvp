"""Атомарный перенос снимков в граф с проверкой обратной сборки до удаления старых таблиц."""

import hashlib
from pathlib import Path

from alembic import op
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.graph_storage import canonical_bytes, load_graph, pack_tree, parsed_artifact, save_graph

revision = "0006_versioned_graph"
down_revision = "0005_generation"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    raw = connection.connection.driver_connection
    sql_dir = Path(__file__).parents[1] / "sql"
    ddl, rules = (
        (sql_dir / "versioned_graph_v08.sql")
        .read_text(encoding="utf-8")
        .split("-- Запись графа сериализуется", 1)
    )
    with raw.cursor(row_factory=dict_row) as cur:
        # Блокировка защищает и от старых воркеров; развёртывание должно остановить их заранее.
        cur.execute("LOCK TABLE programs,program_versions,processing_jobs IN ACCESS EXCLUSIVE MODE")
        cur.execute("CREATE SCHEMA graph_v08_stage")
        cur.execute("SET LOCAL search_path=graph_v08_stage,public")
        cur.execute(ddl)
        cur.execute((sql_dir / "graph_snapshot.sql").read_text(encoding="utf-8"))
        cur.execute(
            "ALTER FUNCTION graph_v08_stage.graph_snapshot(uuid,jsonb) "
            "SET search_path=pg_catalog,graph_v08_stage,pg_temp"
        )
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
        cur.execute("INSERT INTO programs SELECT * FROM public.programs")
        cur.execute(
            "INSERT INTO "
            "source_documents(id,program_id,original_name,format,media_type,size_bytes,"
            "sha256,content,created_at) "
            "SELECT id,program_id,original_name,format,media_type,size_bytes,sha256,"
            "content,created_at FROM public.source_documents"
        )
        cur.execute(
            "INSERT INTO "
            "processing_jobs(id,program_id,input_version_id,output_version_id,operation_key,"
            "request_hash,type,temporal_namespace,workflow_id,parameters,report,created_at) "
            "SELECT id,program_id,input_version_id,output_version_id,operation_key,"
            "request_hash,type,"
            "temporal_namespace,workflow_id,parameters,report,created_at FROM "
            "public.processing_jobs"
        )
        jobs = cur.execute("SELECT * FROM public.processing_jobs ORDER BY created_at,id").fetchall()
        for row in cur.execute("SELECT * FROM public.document_artifacts").fetchall():
            config = {"legacy_config_hash": row["parser_config_hash"]}
            cur.execute(
                "INSERT INTO "
                "document_artifacts(id,program_id,document_id,producing_job_id,producing_attempt,"
                "kind,parser_revision,parser_config,parser_config_hash,hash_encoding,"
                "storage_key,sha256,size_bytes,created_at) "
                "VALUES(%s,%s,%s,%s,1,'markdown',%s,%s,%s,'raw-bytes',%s,%s,%s,%s)",
                (
                    row["id"],
                    row["program_id"],
                    row["document_id"],
                    row["producing_job_id"],
                    row["parser_revision"],
                    Jsonb(config),
                    hashlib.sha256(canonical_bytes(config)).hexdigest(),
                    row["storage_key"],
                    row["sha256"],
                    row["size_bytes"],
                    row["created_at"],
                ),
            )
        for job in jobs:
            if job["type"] != "import":
                continue
            suffix = job["workflow_id"].rsplit("-", 1)[-1]
            attempt = int(suffix) if suffix.isdigit() else 1
            report = job["report"]
            for document, result in report.get("files", {}).items():
                if result.get("output"):
                    artifact = parsed_artifact(
                        cur, job["program_id"], document, result.pop("output"), job["id"], attempt
                    )
                    result["artifact_id"] = str(artifact)
            cur.execute(
                "UPDATE processing_jobs SET report=%s WHERE id=%s", (Jsonb(report), job["id"])
            )
            state = None
            if report.get("snapshot"):
                state = save_graph(
                    cur,
                    job["program_id"],
                    report.pop("snapshot"),
                    producer=job["id"],
                    attempt=attempt,
                    members={
                        item["document_id"]: item["member_name"]
                        for item in job["parameters"].get("documents", [])
                    },
                )
            cur.execute(
                "UPDATE processing_jobs SET report=%s,output_graph_state_id=%s WHERE id=%s",
                (Jsonb(report), state, job["id"]),
            )
        versions = cur.execute(
            "SELECT * FROM public.program_versions ORDER BY program_id,number"
        ).fetchall()
        for version in versions:
            state = save_graph(cur, version["program_id"], version["snapshot"])
            cur.execute(
                "INSERT INTO "
                "program_versions(id,program_id,number,graph_state_id,parent_version_id,"
                "restored_from_version_id,origin,review,operation_key,request_hash,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    version["id"],
                    version["program_id"],
                    version["number"],
                    state,
                    version["parent_version_id"],
                    version["restored_from_version_id"],
                    version["origin"],
                    Jsonb(version["snapshot"]["review"])
                    if version["snapshot"].get("review")
                    else None,
                    version["operation_key"],
                    version["request_hash"],
                    version["created_at"],
                ),
            )
            if load_graph(cur, state, version["snapshot"].get("review")) != version["snapshot"]:
                raise ValueError(
                    f"Версия {version['id']} изменилась при переносе; транзакция отменяется"
                )
        # Исторические авторы не выдумываются; новый журнал заполняется только новыми операциями.
        for job in jobs:
            parameters = job["parameters"]
            report = cur.execute(
                "SELECT report FROM processing_jobs WHERE id=%s", (job["id"],)
            ).fetchone()["report"]
            if report.get("raw_result") is not None:
                from app.audit_storage import save_response

                report["raw_result_artifact"] = save_response(report.pop("raw_result"))
            if report.get("outcome") == "conflict" and "result" in report:
                from app.audit_storage import save_response

                report["result_artifact"] = save_response(report.pop("result"))
            state = cur.execute(
                "SELECT graph_state_id FROM program_versions WHERE id=%s",
                (job["input_version_id"],),
            ).fetchone()["graph_state_id"]
            if job["type"] == "generate" and parameters.get("snapshot"):
                # Старый job закреплял JSON, но не номер исходной попытки импорта.
                # Сохраняем известную задачу и неизвестную попытку, не выдумывая её номер.
                state = save_graph(
                    cur,
                    job["program_id"],
                    parameters.pop("snapshot"),
                    producer=parameters.get("import_job_id"),
                    attempt=None,
                )
            out = None
            if job["output_version_id"]:
                out = cur.execute(
                    "SELECT graph_state_id FROM program_versions WHERE id=%s",
                    (job["output_version_id"],),
                ).fetchone()["graph_state_id"]
            checked = (
                (job["output_version_id"] or job["input_version_id"])
                if job["type"] == "validate" and report.get("outcome") == "completed"
                else None
            )
            cur.execute(
                "UPDATE processing_jobs SET parameters=%s,report=%s,input_graph_state_id=%s,"
                "output_graph_state_id=COALESCE(%s,output_graph_state_id),"
                "checked_version_id=%s WHERE id=%s",
                (
                    Jsonb(pack_tree(cur, job["program_id"], parameters)),
                    Jsonb(pack_tree(cur, job["program_id"], report)),
                    state,
                    out,
                    checked,
                    job["id"],
                ),
            )
        cur.execute("INSERT INTO validation_issues SELECT * FROM public.validation_issues")
        cur.execute(
            "INSERT INTO "
            "generated_packages(job_id,program_id,graph_state_id,version_id,"
            "import_job_id,content,size_bytes,sha256,created_at) "
            "SELECT p.job_id,p.program_id,j.input_graph_state_id,p.version_id,"
            "p.import_job_id,p.content,p.size_bytes,p.sha256,p.created_at "
            "FROM public.generated_packages p JOIN processing_jobs j ON j.id=p.job_id"
        )
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # Перестановка схем внутри транзакции не оставляет окна без проверенных таблиц.
        tables = cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='graph_v08_stage'"
        ).fetchall()
        cur.execute("CREATE SCHEMA graph_v08_old")
        from psycopg import sql

        old_tables = (
            "programs",
            "program_versions",
            "source_documents",
            "version_documents",
            "processing_jobs",
            "document_artifacts",
            "source_refs",
            "source_links",
            "validation_issues",
            "entities",
            "relations",
            "generated_packages",
        )
        dependencies = cur.execute(
            "SELECT conrelid::regclass AS dependent FROM pg_constraint WHERE contype='f' "
            "AND confrelid IN (SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace "
            "AND relname=ANY(%s)) AND conrelid NOT IN (SELECT oid FROM pg_class "
            "WHERE relnamespace='public'::regnamespace AND relname=ANY(%s))",
            (list(old_tables), list(old_tables)),
        ).fetchall()
        if dependencies:
            raise ValueError(
                "Обнаружены внешние ссылки на старые таблицы; требуется отдельный перенос"
            )
        views = cur.execute(
            "SELECT DISTINCT r.ev_class::regclass AS dependent FROM pg_depend d "
            "JOIN pg_rewrite r ON r.oid=d.objid AND d.classid='pg_rewrite'::regclass "
            "WHERE d.refobjid IN (SELECT oid FROM pg_class "
            "WHERE relnamespace='public'::regnamespace AND relname=ANY(%s))",
            (list(old_tables),),
        ).fetchall()
        if views:
            raise ValueError(
                "Обнаружены представления над старой схемой; требуется отдельный перенос"
            )
        for name in old_tables:
            cur.execute(
                sql.SQL("ALTER TABLE public.{} SET SCHEMA graph_v08_old").format(
                    sql.Identifier(name)
                )
            )
        for table in tables:
            cur.execute(
                sql.SQL("ALTER TABLE graph_v08_stage.{} SET SCHEMA public").format(
                    sql.Identifier(table["tablename"])
                )
            )
        cur.execute("ALTER FUNCTION graph_v08_stage.graph_snapshot(uuid,jsonb) SET SCHEMA public")
        cur.execute("SET LOCAL search_path=public")
        cur.execute(
            "ALTER FUNCTION public.graph_snapshot(uuid,jsonb) "
            "SET search_path=pg_catalog,public,pg_temp"
        )
        cur.execute("-- Запись графа сериализуется" + rules)
        cur.execute(
            "CREATE TRIGGER immutable_row BEFORE UPDATE OR DELETE ON validation_issues "
            "FOR EACH ROW EXECUTE FUNCTION graph_reject_mutation()"
        )
        cur.execute(
            "CREATE TRIGGER no_truncate BEFORE TRUNCATE ON validation_issues "
            "FOR EACH STATEMENT EXECUTE FUNCTION graph_reject_mutation()"
        )
        # Проверяем сохранённые ZIP теми же правилами, что и новые результаты.
        cur.execute(
            "CREATE TEMP TABLE graph_package_check (LIKE generated_packages) ON COMMIT DROP"
        )
        cur.execute(
            "CREATE TRIGGER guard_package BEFORE INSERT ON graph_package_check "
            "FOR EACH ROW EXECUTE FUNCTION graph_guard_package()"
        )
        cur.execute("INSERT INTO graph_package_check SELECT * FROM generated_packages")
        cur.execute("DROP TABLE graph_package_check")
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("DROP SCHEMA graph_v08_old CASCADE")
        cur.execute("DROP SCHEMA graph_v08_stage")
        cur.execute("CREATE INDEX graphs_content_idx ON graph_states(program_id,content_hash)")
        cur.execute(
            "CREATE INDEX documents_program_hash_idx ON source_documents(program_id,sha256)"
        )
        cur.execute("CREATE INDEX programs_created_idx ON programs(created_at DESC,id)")
        cur.execute(
            "CREATE INDEX documents_program_created_idx ON "
            "source_documents(program_id,created_at DESC,id)"
        )
        cur.execute(
            "CREATE INDEX parsed_document_hash_idx ON "
            "document_artifacts(program_id,document_id,sha256) WHERE kind='parsed_json'"
        )
        cur.execute(
            "CREATE INDEX processing_import_keys_idx ON processing_jobs USING gin "
            "((parameters->'request_keys')) WHERE type='import'"
        )
        cur.execute(
            "CREATE UNIQUE INDEX processing_import_fingerprint_idx ON "
            "processing_jobs(program_id,(parameters->>'fingerprint')) WHERE type='import'"
        )


def downgrade():
    raise RuntimeError("Откат требует проверенной резервной копии: история графа не удаляется")
