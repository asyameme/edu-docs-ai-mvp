"""Перенос прежних V1/V2, подтверждения, результатов импорта и ZIP без изменения ID."""

import hashlib
import importlib.util
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain import SnapshotData, SnapshotV2
from app.graph_storage import hydrate_job, load_graph
from app.review_logic import content_hash

pytestmark = pytest.mark.skipif(
    not os.getenv("GRAPH_MIGRATION_TEST_DSN"), reason="Нужна отдельная пустая БД миграции"
)


def test_migrate_history_confirmation_import_and_zip(monkeypatch):
    engine = sa.create_engine(os.environ["GRAPH_MIGRATION_TEST_DSN"])
    folder = Path(__file__).parents[1] / "migrations/versions"
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            operations = Operations(MigrationContext.configure(conn))
            for file in sorted(folder.glob("000[1-5]_*.py")):
                spec = importlib.util.spec_from_file_location(file.stem, file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                monkeypatch.setattr(module, "op", operations)
                module.upgrade()
            pid, v1, v2, v3, document, imported, checked, generated, entity, ref = [
                uuid4() for _ in range(10)
            ]
            content = b"original-bytes"
            parsed = {
                "meta": {"doc_type": "RPD", "file_hash": hashlib.sha256(content).hexdigest()},
                "disciplines": [],
            }
            old = {
                "schema_version": 1,
                **SnapshotData(metadata={"legacy": "V1"}).model_dump(mode="json"),
            }
            state = SnapshotV2.model_validate(
                {
                    "metadata": {"parsed_documents": {str(document): parsed}},
                    "document_ids": [str(document)],
                    "entities": [
                        {
                            "id": str(entity),
                            "kind": "discipline",
                            "attributes": {"hours": 72},
                            "source_ref_ids": [str(ref)],
                        }
                    ],
                    "source_refs": [
                        {"id": str(ref), "document_id": str(document), "locator": "page:1"}
                    ],
                }
            )
            source = state.model_dump(mode="json")
            source["source_links"] = [
                {
                    "id": str(uuid4()),
                    "target_kind": "entity",
                    "target_id": str(entity),
                    "field_path": "/attributes/hours",
                    "source_ref_id": str(ref),
                    "observed_value": {"value": 72},
                }
            ]
            pinned = deepcopy(source)
            pinned["entities"][0]["attributes"]["hours"] = 36
            pinned["source_links"][0]["observed_value"]["value"] = 36
            pinned["metadata"]["parsed_documents"][str(document)]["disciplines"] = [
                {"name": "Ранее", "hours": 36}
            ]
            old_generation = uuid4()
            review = {
                "review_id": str(checked),
                "import_job_id": str(imported),
                "analysis_version_id": str(v2),
                "decisions": [],
                "confirmation": None,
            }
            state.review = None
            unconfirmed = {**source, "review": review}
            confirmation = {
                **review,
                "confirmation": {
                    "checked_version_id": str(v2),
                    "check_job_id": str(checked),
                    "content_hash": content_hash(unconfirmed),
                    "accepted_warning_ids": [],
                    "confirmed_at": datetime.now(UTC).isoformat(),
                    "author": None,
                },
            }
            confirmed = {**source, "review": confirmation}
            with conn.connection.driver_connection.cursor(row_factory=dict_row) as cur:
                cur.execute("INSERT INTO programs(id,name) VALUES(%s,%s)", (pid, "Перенос истории"))
                cur.execute(
                    "INSERT INTO program_versions(id,program_id,number,snapshot,"
                    "origin) VALUES(%s,%s,1,%s,'initial')",
                    (v1, pid, Jsonb(old)),
                )
                cur.execute(
                    "INSERT INTO program_versions(id,program_id,number,snapshot,"
                    "parent_version_id,origin) VALUES(%s,%s,2,%s,%s,'save'),(%s,"
                    "%s,3,%s,%s,'save')",
                    (v2, pid, Jsonb(unconfirmed), v1, v3, pid, Jsonb(confirmed), v2),
                )
                cur.execute("UPDATE programs SET current_version_id=%s WHERE id=%s", (v3, pid))
                cur.execute(
                    "INSERT INTO source_documents(id,program_id,original_name,"
                    "format,media_type,size_bytes,sha256,content) VALUES(%s,%s,"
                    "'test.docx','docx','application/octet-stream',%s,%s,%s)",
                    (document, pid, len(content), hashlib.sha256(content).hexdigest(), content),
                )
                for job, kind, parameters, report, input_id, output_id in [
                    (
                        imported,
                        "import",
                        {
                            "documents": [
                                {"document_id": str(document), "member_name": "РПД/test.docx"}
                            ]
                        },
                        {
                            "outcome": "completed",
                            "snapshot": source,
                            "files": {str(document): {"output": parsed}},
                        },
                        v1,
                        None,
                    ),
                    (
                        checked,
                        "validate",
                        {"purpose": "prepare", "input": {"base_snapshot": source}},
                        {"outcome": "completed", "result": {"candidate": unconfirmed}},
                        v1,
                        v2,
                    ),
                    (
                        generated,
                        "generate",
                        {"version_id": str(v3), "documents": []},
                        {"outcome": "completed"},
                        v3,
                        None,
                    ),
                ]:
                    cur.execute(
                        "INSERT INTO processing_jobs(id,program_id,"
                        "input_version_id,output_version_id,operation_key,"
                        "request_hash,type,temporal_namespace,workflow_id,"
                        "parameters,report) VALUES(%s,%s,%s,%s,%s,'test',%s,"
                        "'test',%s,%s,%s)",
                        (
                            job,
                            pid,
                            input_id,
                            output_id,
                            str(job),
                            kind,
                            "import-" + str(job) + "-1" if kind == "import" else str(job),
                            Jsonb(parameters),
                            Jsonb(report),
                        ),
                    )
                cur.execute(
                    "INSERT INTO processing_jobs(id,program_id,input_version_id,"
                    "operation_key,request_hash,type,temporal_namespace,"
                    "workflow_id,parameters,report) "
                    "VALUES(%s,%s,%s,%s,'test','generate','test',%s,%s,'{}')",
                    (
                        old_generation,
                        pid,
                        v1,
                        str(old_generation),
                        str(old_generation),
                        Jsonb(
                            {"snapshot": pinned, "import_job_id": str(imported), "version_id": None}
                        ),
                    ),
                )
                zipped = b"zip-content"
                cur.execute(
                    "INSERT INTO generated_packages(job_id,program_id,version_id,"
                    "content,size_bytes,sha256) VALUES(%s,%s,%s,%s,%s,%s)",
                    (generated, pid, v3, zipped, len(zipped), hashlib.sha256(zipped).hexdigest()),
                )
            file = folder / "0006_versioned_graph.py"
            spec = importlib.util.spec_from_file_location("graph_migration", file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            monkeypatch.setattr(module, "op", operations)
            module.upgrade()
            with conn.connection.driver_connection.cursor(row_factory=dict_row) as cur:
                for version, expected in [(v1, old), (v2, unconfirmed), (v3, confirmed)]:
                    row = cur.execute(
                        "SELECT graph_state_id,review FROM program_versions WHERE id=%s", (version,)
                    ).fetchone()
                    assert load_graph(cur, row["graph_state_id"], row["review"]) == expected
                row = cur.execute(
                    "SELECT * FROM processing_jobs WHERE id=%s", (imported,)
                ).fetchone()
                assert "snapshot" not in row["report"]
                assert hydrate_job(cur, row)["report"]["snapshot"] == source
                assert (
                    cur.execute(
                        "SELECT content FROM generated_packages WHERE job_id=%s", (generated,)
                    ).fetchone()["content"]
                    == zipped
                )
                assert (
                    cur.execute(
                        "SELECT content FROM source_documents WHERE id=%s", (document,)
                    ).fetchone()["content"]
                    == content
                )
                assert (
                    cur.execute(
                        "SELECT checked_version_id FROM processing_jobs WHERE id=%s", (checked,)
                    ).fetchone()["checked_version_id"]
                    == v2
                )
                assert content_hash(confirmed) == content_hash(
                    load_graph(cur, row["output_graph_state_id"], confirmation)
                )
                recovered = cur.execute(
                    "SELECT input_graph_state_id FROM processing_jobs WHERE id=%s",
                    (old_generation,),
                ).fetchone()["input_graph_state_id"]
                assert load_graph(cur, recovered) == pinned
                assert cur.execute(
                    "SELECT producing_job_id,producing_attempt FROM graph_states WHERE id=%s",
                    (recovered,),
                ).fetchone() == {"producing_job_id": imported, "producing_attempt": None}
        finally:
            transaction.rollback()
    engine.dispose()
