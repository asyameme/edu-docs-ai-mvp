"""Публикация исходного графа проверяется в отдельной настоящей PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from psycopg_pool import ConnectionPool
from test_local_document_analysis import formulation_package

from app.document_analysis import analyze_documents
from app.errors import DomainError
from app.import_repository import ImportRepository
from app.repository import Repository
from app.review_contract import DecisionCreate, ReviewCreate
from app.review_projection import validator_result_for_contract
from app.review_repository import ReviewRepository
from app.storage import OriginalStorage

pytestmark = pytest.mark.skipif(
    not os.getenv("GRAPH_TEST_DSN"),
    reason="Нужна отдельная PostgreSQL для графа",
)


def imported_package(pool, documents):
    program = Repository(pool).create_program("Исходные документы")
    storage = OriginalStorage(1024 * 1024)
    stored = []
    for index, document in enumerate(documents):
        name = document["parsed"]["meta"]["file_name"]
        source = storage.save_bytes(name, f"test-original-{index}".encode())
        document["document_id"] = str(source["id"])
        document["parsed"]["meta"]["file_hash"] = source["sha256"]
        stored.append((name, source, True))
    imports = ImportRepository(pool)
    imported, _ = imports.create(program["id"], uuid4().hex, uuid4().hex, stored)
    for document in documents:
        imports.file_finished(
            imported,
            UUID(document["document_id"]),
            1,
            output=document["parsed"],
        )
    imports.finish(imported, 1, "completed", None, [])
    return program, imported


def test_source_graph_is_saved_before_analysis_and_remains_readable_after_its_failure():
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        documents = formulation_package()
        program, imported = imported_package(pool, documents)
        review = ReviewRepository(pool)
        review_id = review.create(
            program["id"],
            uuid4().hex,
            ReviewCreate(
                import_job_id=imported,
                expected_version_id=program["current_version_id"],
            ),
        )

        source_id = review.ensure_source_graph(review_id)
        assert review.ensure_source_graph(review_id) == source_id
        assert review.job_by_id(review_id)["report"].get("outcome") is None
        review.fail(review_id, f"review-{review_id}", "unavailable", "Анализ недоступен")

        assert Repository(pool).program(program["id"])["current_version_id"] == source_id
        details = review.details(program["id"], source_id, 100, 0)
        assert details["version"]["snapshot"]["review"]["graph_ready"]
        assert details["prepare_report"]["coverage"] == []
        assert review.job_by_id(review_id)["report"]["outcome"] == "failed"


def test_import_queues_one_preparation_without_an_open_browser(monkeypatch):
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        program, imported = imported_package(pool, formulation_package())
        imports = ImportRepository(pool)
        with pool.connection() as conn:
            jobs = conn.execute(
                "SELECT id FROM processing_jobs WHERE program_id=%s "
                "AND parameters->>'purpose'='prepare'",
                (program["id"],),
            ).fetchall()
        assert len(jobs) == 1
        review_id = jobs[0][0]
        assert imports.read(program["id"], imported)["automatic_review_id"] == review_id
        imports.finish(imported, 1, "completed", None, [])
        review = ReviewRepository(pool)
        request = ReviewCreate(
            import_job_id=imported,
            expected_version_id=program["current_version_id"],
        )
        assert (
            review.create(
                program["id"],
                uuid4().hex,
                request,
            )
            == review_id
        )
        with pool.connection() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM processing_jobs WHERE program_id=%s "
                    "AND parameters->>'purpose'='prepare'",
                    (program["id"],),
                ).fetchone()[0]
                == 1
            )


def test_publish_keeps_original_graph_before_first_decision_and_is_idempotent():
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        documents = formulation_package()
        program, imported = imported_package(pool, documents)
        review = ReviewRepository(pool)
        review_id = review.create(
            program["id"],
            uuid4().hex,
            ReviewCreate(
                import_job_id=imported,
                expected_version_id=program["current_version_id"],
            ),
        )
        result = validator_result_for_contract(analyze_documents(documents, review_id))
        assert any(item["requires_decision"] for item in result["issues"])
        assert review.publish(review_id, deepcopy(result)) == "completed"
        current = Repository(pool).program(program["id"])["current_version_id"]
        first = Repository(pool).version(program["id"], current)

        assert first["snapshot"]["review"]["graph_ready"]
        assert first["snapshot"]["review"]["lifecycle_policy"] == "automatic_source_v2"
        assert first["snapshot"]["review"]["decisions"] == []
        assert first["snapshot"]["review"]["confirmation"] is None
        assert {
            item["attributes"]["formulation"]
            for item in first["snapshot"]["entities"]
            if item["kind"] == "competency"
        } == {"Формулировка 0", "Формулировка 1"}
        assert review.publish(review_id, deepcopy(result)) == "completed"
        assert Repository(pool).program(program["id"])["current_version_id"] == current
        with pool.connection() as conn:
            graph = conn.execute(
                "SELECT graph_state_id FROM program_versions WHERE id=%s",
                (current,),
            ).fetchone()[0]
            assert graph is not None
            count = conn.execute(
                "SELECT count(*) FROM program_versions WHERE program_id=%s",
                (program["id"],),
            ).fetchone()[0]
            assert count == 2

        conflict = next(item for item in result["issues"] if item["requires_decision"])
        selected = conflict["variants"][0]
        key = uuid4().hex
        body = DecisionCreate(
            expected_version_id=current,
            conflict_id=conflict["id"],
            variant_id=selected["id"],
            signature="Тест",
        )
        changed = review.decide(program["id"], review_id, key, body)
        assert changed["snapshot"]["review"]["graph_ready"]
        assert changed["snapshot"]["review"]["confirmation"] is None
        assert changed["id"] != current
        assert review.decide(program["id"], review_id, key, body)["id"] == changed["id"]
        assert Repository(pool).version(program["id"], current)["snapshot"] == first["snapshot"]
        with pool.connection() as conn:
            checks = conn.execute(
                "SELECT count(*) FROM processing_jobs WHERE program_id=%s "
                "AND input_version_id=%s AND parameters->>'purpose'='check'",
                (program["id"], changed["id"]),
            ).fetchone()[0]
            assert checks == 1


def test_failed_analysis_retry_keeps_source_and_newer_versions():
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        program, imported = imported_package(pool, formulation_package())
        review = ReviewRepository(pool)
        request = ReviewCreate(import_job_id=imported,
                               expected_version_id=program["current_version_id"])
        job = review.create(program["id"], uuid4().hex, request)
        source = review.ensure_source_graph(job)
        review.fail(job, f"review-{job}", "offline", "Недоступен анализ")
        newer = Repository(pool).save(program["id"], source, uuid4().hex, restore_id=source)
        retry = ReviewCreate(import_job_id=imported, expected_version_id=newer["id"],
                             rerun_same_import=True)
        key = uuid4().hex
        assert review.create(program["id"], key, retry) == job
        assert review.create(program["id"], key, retry) == job
        assert review.job_by_id(job)["workflow_id"] != f"review-{job}"
        assert review.ensure_source_graph(job) == source
        assert Repository(pool).program(program["id"])["current_version_id"] == newer["id"]
        review.fail(job, f"review-{job}", "late", "Поздний отказ старой попытки")
        assert "outcome" not in review.job_by_id(job)["report"]
        stale_result = validator_result_for_contract(analyze_documents(formulation_package(), job))
        review.publish(job, stale_result, expected_workflow_id=f"review-{job}")
        assert "outcome" not in review.job_by_id(job)["report"]


def test_restore_of_automatic_version_preserves_policy_and_queues_exact_check():
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        program, imported = imported_package(pool, formulation_package())
        review = ReviewRepository(pool)
        job = review.create(program["id"], uuid4().hex, ReviewCreate(
            import_job_id=imported, expected_version_id=program["current_version_id"]))
        source = review.ensure_source_graph(job)
        restored = Repository(pool).save(program["id"], source, uuid4().hex, restore_id=source)
        assert restored["snapshot"]["review"]["lifecycle_policy"] == "automatic_source_v2"
        details = review.details(program["id"], restored["id"], 100, 0)
        assert details["lifecycle"]["check_job_id"]
        assert not details["lifecycle"]["can_generate"]
        with pytest.raises(DomainError):
            Repository(pool).save(program["id"], source, uuid4().hex, restore_id=source)


def test_both_chat_read_paths_show_incomplete_saved_analysis_and_match_ui():
    from app.chat_findings_store import read_findings_page
    from worker.agent.read_adapter import PostgresChatReadPort

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        program, imported = imported_package(pool, formulation_package())
        review = ReviewRepository(pool)
        job = review.create(program["id"], uuid4().hex, ReviewCreate(
            import_job_id=imported, expected_version_id=program["current_version_id"]))
        source = review.ensure_source_graph(job)
        details = review.details(program["id"], source, 100, 0)
        page = read_findings_page(pool, program["id"], source)
        individual = PostgresChatReadPort(pool)._get_validation_findings(program["id"], source, 20)
        assert len(details["issues"]) == page.total_records == len(individual) == 1
        assert individual[0].rule_code == "VERSION_ANALYSIS_STATUS"
        assert page.groups[0].rule_code == "VERSION_ANALYSIS_STATUS"


def test_late_analysis_cannot_overwrite_a_newer_version_or_a_retry():
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        documents = formulation_package()
        program, imported = imported_package(pool, documents)
        review = ReviewRepository(pool)
        job = review.create(program["id"], uuid4().hex, ReviewCreate(
            import_job_id=imported, expected_version_id=program["current_version_id"]))
        source = review.ensure_source_graph(job)
        newer = Repository(pool).save(program["id"], source, uuid4().hex, restore_id=source)
        result = validator_result_for_contract(analyze_documents(documents, job))
        assert review.publish(job, deepcopy(result)) == "completed"
        assert Repository(pool).program(program["id"])["current_version_id"] == newer["id"]


def test_concurrent_publication_and_choices_cannot_lose_or_duplicate_versions(monkeypatch):
    from app import source_graph

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        documents = formulation_package()
        program, imported = imported_package(pool, documents)
        review = ReviewRepository(pool)
        job = review.create(program["id"], uuid4().hex, ReviewCreate(
            import_job_id=imported, expected_version_id=program["current_version_id"]))
        with ThreadPoolExecutor(max_workers=2) as executor:
            source_ids = list(executor.map(lambda _: review.ensure_source_graph(job), range(2)))
        assert source_ids[0] == source_ids[1]
        source = source_ids[0]
        result = validator_result_for_contract(analyze_documents(documents, job))
        review.publish(job, result)
        issue = next(item for item in result["issues"] if item["requires_decision"])
        barrier, apply = Barrier(2), source_graph.apply_document_selections

        def synchronized(*args):
            next_snapshot = apply(*args)
            barrier.wait(timeout=10)
            return next_snapshot

        monkeypatch.setattr(source_graph, "apply_document_selections", synchronized)

        def choose(index):
            try:
                return review.decide(program["id"], job, uuid4().hex, DecisionCreate(
                    expected_version_id=source, conflict_id=issue["id"],
                    variant_id=issue["variants"][index]["id"], signature="Одновременный выбор"))
            except DomainError as error:
                assert error.status == 409
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            versions = list(executor.map(choose, [0, 1]))
        winner = [version for version in versions if version]
        assert len(winner) == 1
        assert Repository(pool).program(program["id"])["current_version_id"] == winner[0]["id"]
        saved_source = Repository(pool).version(program["id"], source)["snapshot"]
        assert saved_source["review"]["decisions"] == []
        with pool.connection() as conn:
            assert conn.execute("SELECT count(*) FROM program_versions WHERE program_id=%s",
                                (program["id"],)).fetchone()[0] == 3


def test_multiple_document_projections_keep_distinct_immutable_artifacts():
    from test_source_choices import chat_change, choice

    from app.domain import SnapshotV2
    from app.source_graph import apply_document_selections
    from app.validation_bridge import project_snapshot_documents

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        documents = formulation_package()
        program, imported = imported_package(pool, documents)
        review = ReviewRepository(pool)
        job = review.create(program["id"], uuid4().hex, ReviewCreate(
            import_job_id=imported, expected_version_id=program["current_version_id"]))
        source = review.ensure_source_graph(job)
        original = SnapshotV2.model_validate(
            Repository(pool).version(program["id"], source)["snapshot"],
        )
        first_snapshot = apply_document_selections(program["id"], documents, original,
            [choice(original, documents[0]["document_id"])])
        first = Repository(pool).save(program["id"], source, uuid4().hex, snapshot=first_snapshot)
        target = next(item for item in first_snapshot.entities if item.kind == "competency")
        changed = chat_change(first_snapshot, target.id, "/attributes/formulation", "После чата")
        second_snapshot = apply_document_selections(program["id"], documents, changed, [])
        second = Repository(pool).save(program["id"], first["id"], uuid4().hex,
                                       snapshot=second_snapshot)
        loaded = SnapshotV2.model_validate(
            Repository(pool).version(program["id"], second["id"])["snapshot"],
        )
        projected = project_snapshot_documents(documents, loaded)[0]["parsed"]
        assert projected["competencies"][0]["formulation"] == "После чата"
        assert Repository(pool).version(program["id"], first["id"])["snapshot"] == first["snapshot"]


def test_resolved_source_relation_agrees_in_ui_summary_and_individual_chat_findings():
    from document_cases import payload
    from test_source_choices import choice

    from app.chat_findings_store import read_findings_page
    from app.domain import SnapshotV2
    from app.source_graph import apply_document_selections
    from worker.agent.read_adapter import PostgresChatReadPort

    parsed = payload("RPD")
    parsed["competencies"] = [{"code": "УК-1", "formulation": "Описание",
                               "source_ref": {"file": "source.docx", "paragraph_index": 3}}]
    parsed["relations"] = [{"src_entity": "discipline", "src_key": "Дисциплина",
                             "dst_entity": "competency", "dst_key": "УК-X", "kind": "формирует",
                             "source_ref": parsed["disciplines"][0]["source_ref"]}]
    documents = [{"document_id": str(uuid4()), "parsed": parsed}]
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        program, imported = imported_package(pool, documents)
        review = ReviewRepository(pool)
        job = review.create(program["id"], uuid4().hex, ReviewCreate(
            import_job_id=imported, expected_version_id=program["current_version_id"]))
        source = review.ensure_source_graph(job)
        original = SnapshotV2.model_validate(
            Repository(pool).version(program["id"], source)["snapshot"],
        )
        selection = choice(original, documents[0]["document_id"]).model_copy(update={"changes": [{
            "op": "set_competency_links", "document_id": documents[0]["document_id"],
            "collection": "disciplines", "record_index": 0,
            "expected_codes": ["УК-X"], "codes": ["УК-1"],
        }]})
        fixed = apply_document_selections(program["id"], documents, original, [selection])
        version = Repository(pool).save(program["id"], source, uuid4().hex, snapshot=fixed)
        assert not fixed.metadata["unresolved_relations"]
        assert any(item.type == "FORMS_COMPETENCY" for item in fixed.relations)
        details = review.details(program["id"], version["id"], 100, 0)
        page = read_findings_page(pool, program["id"], version["id"])
        individual = PostgresChatReadPort(pool)._get_validation_findings(
            program["id"], version["id"], 20,
        )
        assert next(item for item in details["issues"] if item["rule_code"] ==
                    "SOURCE_RELATION_UNRESOLVED")["effective_status"] == "resolved"
        assert next(item for item in page.groups if item.rule_code ==
                    "SOURCE_RELATION_UNRESOLVED").status_counts == {"resolved": 1}
        assert next(item for item in individual if item.rule_code ==
                    "SOURCE_RELATION_UNRESOLVED").status == "resolved"
        assert original.metadata["unresolved_relations"]


@pytest.mark.parametrize("mode", ["single", "batch", "custom"])
def test_deferred_choices_reuse_graph_until_one_explicit_checkpoint(mode):
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        documents = formulation_package()
        program, imported = imported_package(pool, documents)
        review = ReviewRepository(pool)
        job = review.create(
            program["id"],
            uuid4().hex,
            ReviewCreate(import_job_id=imported, expected_version_id=program["current_version_id"]),
        )
        result = validator_result_for_contract(analyze_documents(documents, job))
        assert review.publish(job, deepcopy(result)) == "completed"
        repo = Repository(pool)
        source = repo.program(program["id"])["current_version_id"]
        conflict = next(item for item in result["issues"] if item["requires_decision"])
        body = DecisionCreate(
            expected_version_id=source,
            conflict_id=conflict["id"],
            variant_id=conflict["variants"][0]["id"],
            signature="Тест",
        )
        assert review.checkpoint_choices(program["id"], job, source)["graph_build"] is None
        key = uuid4().hex
        if mode == "custom":
            body = DecisionCreate(
                expected_version_id=source,
                conflict_id=conflict["id"],
                custom_value="Собственный текст пользователя",
                signature="Тест",
            )

        def save():
            if mode == "batch":
                from app.review_contract import DecisionBatchCreate

                batch = DecisionBatchCreate(
                    expected_version_id=source,
                    signature="Тест",
                    selections=[
                        {"conflict_id": body.conflict_id, "variant_id": body.variant_id},
                    ],
                )
                return review.decide_many(program["id"], job, key, batch, defer_graph=True)
            return review.decide(program["id"], job, key, body, defer_graph=True)

        pending = save()
        assert save()["id"] == pending["id"]
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT graph_state_id FROM program_versions WHERE id=ANY(%s)",
                ([source, pending["id"]],),
            ).fetchall()
            assert rows[0][0] == rows[1][0]
        details = review.details(program["id"], pending["id"], 100, 0)
        assert details["lifecycle"]["needs_projection"]
        assert not details["lifecycle"]["can_generate"]
        assert any(item["decision"] for item in details["issues"])
        with pytest.raises(DomainError) as stale:
            review.decide(program["id"], job, uuid4().hex, body, defer_graph=True)
        assert stale.value.status == 409
        checkpoint = review.checkpoint_choices(program["id"], job, pending["id"])
        assert review.checkpoint_choices(program["id"], job, pending["id"]) == checkpoint
        built = review.build_graph(UUID(checkpoint["graph_build"]))
        assert built not in {"failed", "conflict"}
        assert review.build_graph(UUID(checkpoint["graph_build"])) == built
        final = repo.version(program["id"], UUID(built))["snapshot"]
        assert final["review"]["projected_decision_ids"] == [conflict["id"]]
        assert not review.details(program["id"], UUID(built), 100, 0)["lifecycle"][
            "needs_projection"
        ]
        assert repo.version(program["id"], source)["snapshot"]["review"]["decisions"] == []

        second_body = body.model_copy(
            update={
                "expected_version_id": UUID(built),
                "variant_id": UUID(conflict["variants"][1]["id"]),
                "custom_value": None,
            }
        )
        second = review.decide(program["id"], job, uuid4().hex, second_body, defer_graph=True)
        second_job = review.checkpoint_choices(program["id"], job, second["id"])
        rebuilt = review.build_graph(UUID(second_job["graph_build"]))
        assert rebuilt not in {"failed", "conflict"}
        last = repo.version(program["id"], UUID(rebuilt))["snapshot"]
        assert len(last["review"]["decisions"]) == 1
        assert last["entities"] != final["entities"]
