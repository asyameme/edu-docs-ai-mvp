"""Тесты проекций версии графа в документные JSON (фаза graph_validation)."""

import json
from uuid import UUID, uuid4

import pytest

from app.domain import Entity, RelationV2, SnapshotV2, SourceLink, SourceRefV2
from worker.agent.graph_projections import (
    GraphProjectionError,
    document_projections,
)


def _mini_program_snapshot() -> tuple[SnapshotV2, dict]:
    """Мини-программа: учебный план + РПД одной дисциплины с компетенцией.

    Повторяет связку «парсер → document_adapter»: сущности по-документные,
    атрибуты — это записи без source_ref, локаторы — канонический JSON ссылки.
    """

    doc_cur, doc_rpd = uuid4(), uuid4()
    discipline_rpd, curriculum_item = uuid4(), uuid4()
    competency_cur, competency_rpd, indicator_rpd = uuid4(), uuid4(), uuid4()
    ref_cur, ref_rpd = uuid4(), uuid4()
    ref_comp_cur, ref_comp_rpd, ref_ind_rpd = uuid4(), uuid4(), uuid4()
    ref_rel_cur, ref_rel_rpd = uuid4(), uuid4()

    locator_cur = {"file": "план.docx", "section": "Раздел 2", "table": 1, "row": 1, "col": 2}
    locator_rpd = {"file": "рпд.docx", "section": "1.2;2.1", "paragraph_index": 1}

    def ref(identifier: UUID, document_id: UUID, payload: dict) -> SourceRefV2:
        return SourceRefV2(
            id=identifier,
            document_id=document_id,
            section=payload.get("section"),
            locator=json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        )

    parsed_curriculum = {
        "meta": {
            "schema_version": 2,
            "doc_type": "CURRICULUM",
            "file_name": "план.docx",
            "program_id": "26/5003/1",
        },
        "curriculum_items": [
            {
                "source_ref": locator_cur,
                "discipline_name": "Математический анализ I",
                "semester": 1,
                "credit_units": 3,
                "hours_total": 108,
                "block": "Блок.1.дисц",
            }
        ],
        "competencies": [
            {
                "source_ref": {
                    "file": "план.docx", "section": "Раздел 2", "table": 1, "row": 1, "col": 1
                },
                "code": "УК-1",
                "formulation": "Способен осуществлять поиск и анализ информации",
            }
        ],
        "relations": [
            {
                "source_ref": {
                    "file": "план.docx", "section": "Раздел 2", "table": 1, "row": 1, "col": 3
                },
                "src_entity": "curriculum_item",
                "src_key": "Математический анализ I",
                "dst_entity": "competency",
                "dst_key": "УК-1",
                "kind": "формирует",
            }
        ],
    }
    parsed_rpd = {
        "meta": {
            "schema_version": 2,
            "doc_type": "RPD",
            "file_name": "рпд.docx",
            "program_id": "26/5003/1",
        },
        "title_credit_units": 3,
        "disciplines": [
            {
                "source_ref": locator_rpd,
                "name": "Математический анализ I",
                "semesters": [1],
                "credit_units": 3,
                "hours_total": 108,
            }
        ],
        "competencies": [
            {
                "source_ref": {
                    "file": "рпд.docx", "section": "1.3", "table": 1, "row": 2, "col": 2
                },
                "code": "УК-1",
                "formulation": "Способен осуществлять поиск и анализ информации",
            }
        ],
        "indicators": [
            {
                "source_ref": {
                    "file": "рпд.docx", "section": "1.3", "table": 1, "row": 2, "col": 3
                },
                "code": "УК-1.1",
                "text": "Анализирует задачу",
                "competency_code": "УК-1",
            }
        ],
        "relations": [
            {
                "source_ref": {
                    "file": "рпд.docx", "section": "1.3", "table": 1, "row": 2, "col": 1
                },
                "src_entity": "discipline",
                "src_key": "Математический анализ I",
                "dst_entity": "competency",
                "dst_key": "УК-1",
                "kind": "формирует",
            }
        ],
    }

    snapshot = SnapshotV2(
        document_ids=[doc_cur, doc_rpd],
        metadata={
            "program_id": str(uuid4()),
            "parsed_documents": {str(doc_cur): parsed_curriculum, str(doc_rpd): parsed_rpd},
        },
        entities=[
            Entity(
                id=curriculum_item,
                kind="curriculum_item",
                attributes={
                    key: value
                    for key, value in parsed_curriculum["curriculum_items"][0].items()
                    if key != "source_ref"
                },
                source_ref_ids=[ref_cur],
            ),
            Entity(
                id=discipline_rpd,
                kind="discipline",
                attributes={
                    key: value
                    for key, value in parsed_rpd["disciplines"][0].items()
                    if key != "source_ref"
                },
                source_ref_ids=[ref_rpd],
            ),
            Entity(
                id=competency_cur,
                kind="competency",
                attributes={
                    key: value
                    for key, value in parsed_curriculum["competencies"][0].items()
                    if key != "source_ref"
                },
                source_ref_ids=[ref_comp_cur],
            ),
            Entity(
                id=competency_rpd,
                kind="competency",
                attributes={
                    key: value
                    for key, value in parsed_rpd["competencies"][0].items()
                    if key != "source_ref"
                },
                source_ref_ids=[ref_comp_rpd],
            ),
            Entity(
                id=indicator_rpd,
                kind="indicator",
                attributes={
                    key: value
                    for key, value in parsed_rpd["indicators"][0].items()
                    if key != "source_ref"
                },
                source_ref_ids=[ref_ind_rpd],
            ),
        ],
        relations=[
            RelationV2(
                id=uuid4(),
                source_id=curriculum_item,
                target_id=competency_cur,
                type="FORMS_COMPETENCY",
                attributes={
                    key: value
                    for key, value in parsed_curriculum["relations"][0].items()
                    if key != "source_ref"
                },
                source_ref_ids=[ref_rel_cur],
            ),
            RelationV2(
                id=uuid4(),
                source_id=discipline_rpd,
                target_id=competency_rpd,
                type="FORMS_COMPETENCY",
                attributes={
                    key: value
                    for key, value in parsed_rpd["relations"][0].items()
                    if key != "source_ref"
                },
                source_ref_ids=[ref_rel_rpd],
            ),
        ],
        source_refs=[
            ref(ref_cur, doc_cur, locator_cur),
            ref(ref_rpd, doc_rpd, locator_rpd),
            ref(ref_comp_cur, doc_cur, parsed_curriculum["competencies"][0]["source_ref"]),
            ref(ref_comp_rpd, doc_rpd, parsed_rpd["competencies"][0]["source_ref"]),
            ref(ref_ind_rpd, doc_rpd, parsed_rpd["indicators"][0]["source_ref"]),
            ref(ref_rel_cur, doc_cur, parsed_curriculum["relations"][0]["source_ref"]),
            ref(ref_rel_rpd, doc_rpd, parsed_rpd["relations"][0]["source_ref"]),
        ],
        source_links=[
            SourceLink(
                id=uuid4(),
                target_kind="entity",
                target_id=discipline_rpd,
                field_path="/attributes/hours_total",
                source_ref_id=ref_rpd,
                observed_value={"value": 108},
            ),
            SourceLink(
                id=uuid4(),
                target_kind="entity",
                target_id=discipline_rpd,
                field_path="/attributes/credit_units",
                source_ref_id=ref_rpd,
                observed_value={"value": 3},
            ),
        ],
    )
    return snapshot, {
        "doc_cur": doc_cur,
        "doc_rpd": doc_rpd,
        "discipline": discipline_rpd,
        "curriculum_item": curriculum_item,
        "ref_rpd": ref_rpd,
        "locator_cur": locator_cur,
        "locator_rpd": locator_rpd,
        "parsed_curriculum": parsed_curriculum,
        "parsed_rpd": parsed_rpd,
    }


@pytest.fixture
def program():
    return _mini_program_snapshot()


def test_projections_follow_document_order_and_meta(program):
    snapshot, data = program
    projections = document_projections(snapshot)
    assert [doc["meta"]["doc_type"] for doc in projections] == ["CURRICULUM", "RPD"]
    assert [doc["meta"]["file_name"] for doc in projections] == ["план.docx", "рпд.docx"]


def test_projection_records_match_parsed_documents(program):
    snapshot, data = program
    curriculum, rpd = document_projections(snapshot)
    assert curriculum["curriculum_items"][0]["hours_total"] == 108
    assert rpd["disciplines"][0]["name"] == "Математический анализ I"
    assert rpd["title_credit_units"] == 3
    assert rpd["relations"][0]["kind"] == "формирует"


def test_edited_attribute_is_synced_into_projection(program):
    snapshot, data = program
    edited = snapshot.model_copy(deep=True)
    entity = next(item for item in edited.entities if item.id == data["discipline"])
    entity.attributes["hours_total"] = 54
    curriculum, rpd = document_projections(edited)
    assert rpd["disciplines"][0]["hours_total"] == 54
    assert curriculum["curriculum_items"][0]["hours_total"] == 108
    assert snapshot.entities[1].attributes["hours_total"] == 108


def test_relation_attributes_are_synced(program):
    snapshot, data = program
    edited = snapshot.model_copy(deep=True)
    relation = next(
        item
        for item in edited.relations
        if item.attributes.get("src_entity") == "curriculum_item"
    )
    relation.attributes["kind"] = "формирует"
    curriculum, _ = document_projections(edited)
    assert curriculum["relations"][0]["src_key"] == "Математический анализ I"
    assert curriculum["relations"][0]["kind"] == "формирует"


def test_snapshot_without_parsed_documents_is_rejected():
    snapshot, _ = _mini_program_snapshot()
    bare = snapshot.model_copy(update={"metadata": {"program_id": "x"}}, deep=True)
    with pytest.raises(GraphProjectionError, match="parsed_documents"):
        document_projections(bare)
