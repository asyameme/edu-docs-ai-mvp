"""Тесты моста «ChangeProposal → правка графа + патчи документов»."""

import json
from uuid import UUID, uuid4

import pytest
from test_generation_docx_patch import _paragraph, _table, document_text, make_docx

from app.domain import Entity, RelationV2, SnapshotV2, SourceLink, SourceRefV2
from worker.agent.contracts import ChangeProposal, ProposedAttributeChange
from worker.agent.proposal_apply import (
    ProposalApplicationError,
    apply_change,
    generation_inputs,
    normalize_field_path,
    plan_approved_changes,
    touched_document_ids,
)
from worker.generation import (
    FieldChange,
    GenerationMappingError,
    OriginalDocument,
    build_generation_documents,
    patch_docx_bytes,
)


def _uuid():
    return uuid4()


@pytest.fixture
def snapshot():
    entity_id, relation_id = _uuid(), _uuid()
    ref_plan, ref_rpd = _uuid(), _uuid()
    doc_plan, doc_rpd = _uuid(), _uuid()
    return (
        SnapshotV2(
            entities=[
                Entity(
                    id=entity_id,
                    kind="discipline",
                    attributes={"name": "Технологии MLOps", "hours": 108},
                    source_ref_ids=[ref_plan, ref_rpd],
                )
            ],
            relations=[
                RelationV2(
                    id=relation_id,
                    source_id=entity_id,
                    target_id=entity_id,
                    type="HAS_LOAD",
                    attributes={"hours": 72},
                    source_ref_ids=[ref_rpd],
                )
            ],
            source_refs=[
                SourceRefV2(
                    id=ref_plan,
                    document_id=doc_plan,
                    section="Раздел 2",
                    locator=json.dumps({"table": 1, "row": 1, "col": 2}),
                ),
                SourceRefV2(
                    id=ref_rpd,
                    document_id=doc_rpd,
                    section=None,
                    locator=json.dumps({"paragraph_index": 1}),
                ),
            ],
            document_ids=[doc_plan, doc_rpd],
            source_links=[
                SourceLink(
                    id=_uuid(),
                    target_kind="entity",
                    target_id=entity_id,
                    field_path="/attributes/hours",
                    source_ref_id=ref_plan,
                    observed_value={"value": 108, "unit": "ч"},
                ),
                SourceLink(
                    id=_uuid(),
                    target_kind="entity",
                    target_id=entity_id,
                    field_path="/attributes/hours",
                    source_ref_id=ref_rpd,
                    observed_value={"value": 108, "unit": "ч"},
                ),
                SourceLink(
                    id=_uuid(),
                    target_kind="relation",
                    target_id=relation_id,
                    field_path="/attributes/hours",
                    source_ref_id=ref_rpd,
                    observed_value={"value": 72, "unit": "ч"},
                ),
            ],
        ),
        {"entity": entity_id, "relation": relation_id, "ref_plan": ref_plan, "ref_rpd": ref_rpd,
         "doc_plan": doc_plan, "doc_rpd": doc_rpd},
    )


def _proposal(snapshot_data, *, field="hours", expected=108, new=54, refs=None, target="entity"):
    if isinstance(target, UUID):
        target_id, target_kind = target, "entity"
    else:
        target_id = snapshot_data[target]
        target_kind = "entity" if target == "entity" else "relation"
    return ChangeProposal(
        kind="change_proposal",
        summary="Сократить трудоёмкость",
        changes=[
            ProposedAttributeChange(
                target_kind=target_kind,
                target_id=target_id,
                field_path=field,
                expected_old_value=expected,
                new_value=new,
                reason="По решению учёного совета",
                source_ref_ids=refs
                if refs is not None
                else [snapshot_data["ref_plan"], snapshot_data["ref_rpd"]],
            )
        ],
    )


def test_normalize_field_path_forms():
    assert normalize_field_path("hours") == "/attributes/hours"
    assert normalize_field_path("/attributes/hours") == "/attributes/hours"
    assert normalize_field_path("attributes.hours") == "/attributes/hours"
    assert normalize_field_path("hours~1total") == "/attributes/hours~1total"
    for bad in ("$", "/source_ref_ids/0", "hours/", "a~2b", " "):
        with pytest.raises(ProposalApplicationError):
            normalize_field_path(bad)


def test_plan_approves_change_and_normalizes_path(snapshot):
    snap, data = snapshot
    approved = plan_approved_changes(snap, _proposal(data))
    assert len(approved) == 1
    assert approved[0].field_path == "/attributes/hours"
    assert approved[0].evidence_ref_ids == (data["ref_plan"], data["ref_rpd"])


def test_plan_rejects_stale_expected_value(snapshot):
    snap, data = snapshot
    with pytest.raises(ProposalApplicationError, match="агент видел"):
        plan_approved_changes(snap, _proposal(data, expected=999))


def test_plan_allows_numeric_json_forms(snapshot):
    snap, data = snapshot
    approved = plan_approved_changes(snap, _proposal(data, expected=108.0))
    assert approved[0].expected_old_value == 108.0


def test_plan_rejects_missing_field_and_unknown_target(snapshot):
    snap, data = snapshot
    with pytest.raises(ProposalApplicationError, match="нет в текущей версии"):
        plan_approved_changes(snap, _proposal(data, field="zet"))
    with pytest.raises(ProposalApplicationError, match="отсутствует в выбранной версии"):
        plan_approved_changes(snap, _proposal(data, target=_uuid()))


def test_plan_rejects_when_no_source_matches_evidence(snapshot):
    snap, data = snapshot
    with pytest.raises(ProposalApplicationError, match="ни один из источников"):
        plan_approved_changes(snap, _proposal(data, refs=[_uuid()]))


def test_plan_allows_reference_source_alongside_evidence(snapshot):
    snap, data = snapshot
    # Агент прикладывает источник-эталон (учебный план), не являющийся
    # свидетельством поля: применение не блокируется.
    approved = plan_approved_changes(
        snap,
        _proposal(data, refs=[data["ref_rpd"], _uuid()]),
    )
    assert approved[0].evidence_ref_ids == (data["ref_plan"], data["ref_rpd"])


def test_plan_collects_all_errors_at_once(snapshot):
    snap, data = snapshot
    stale = _proposal(data, expected=1)
    unverified = _proposal(data, refs=[_uuid()], field="hours", expected=108)
    combined = ChangeProposal(
        kind="change_proposal",
        summary="Две плохие правки",
        changes=[*stale.changes, *unverified.changes],
    )
    with pytest.raises(ProposalApplicationError) as excinfo:
        plan_approved_changes(snap, combined)
    assert "агент видел" in str(excinfo.value)
    assert "ни один из источников" in str(excinfo.value)


def test_plan_collapses_duplicates_and_rejects_conflicts(snapshot):
    snap, data = snapshot
    same = ChangeProposal(
        kind="change_proposal",
        summary="Дубль",
        changes=[_proposal(data).changes[0], _proposal(data).changes[0]],
    )
    assert len(plan_approved_changes(snap, same)) == 1
    conflicting = ChangeProposal(
        kind="change_proposal",
        summary="Конфликт",
        changes=[_proposal(data, new=54).changes[0], _proposal(data, new=72).changes[0]],
    )
    with pytest.raises(ProposalApplicationError, match="противоречивые"):
        plan_approved_changes(snap, conflicting)


def test_plan_supports_relation_target(snapshot):
    snap, data = snapshot
    approved = plan_approved_changes(
        snap, _proposal(data, target="relation", expected=72, refs=[data["ref_rpd"]])
    )
    assert approved[0].target_kind == "relation"


def test_apply_change_returns_new_snapshot(snapshot):
    snap, data = snapshot
    approved = plan_approved_changes(snap, _proposal(data))
    updated = apply_change(snap, approved[0])
    entity = next(item for item in updated.entities if item.id == data["entity"])
    assert entity.attributes["hours"] == 54
    assert entity.attributes["name"] == "Технологии MLOps"
    original = next(item for item in snap.entities if item.id == data["entity"])
    assert original.attributes["hours"] == 108
    assert len(updated.source_links) == len(snap.source_links)
    assert updated.source_links is snap.source_links
    assert updated.source_refs is snap.source_refs
    assert updated.relations is snap.relations
    assert updated.entities[0] is not snap.entities[0]
    assert updated.entities[0].attributes is not snap.entities[0].attributes


def test_graph_validation_serializes_each_evidence_target_once(snapshot, monkeypatch):
    snap, data = snapshot
    raw = snap.model_dump(mode="json")
    original_dump = Entity.model_dump
    dumped_targets = []

    def counted_dump(self, *args, **kwargs):
        if self.id == data["entity"]:
            dumped_targets.append(self.id)
        return original_dump(self, *args, **kwargs)

    monkeypatch.setattr(Entity, "model_dump", counted_dump)
    SnapshotV2.model_validate(raw)

    assert dumped_targets == [data["entity"]]


def test_generation_inputs_touch_all_evidence_documents(snapshot):
    snap, data = snapshot
    approved = plan_approved_changes(snap, _proposal(data))
    changes, links, refs = generation_inputs(snap, approved)
    assert changes == [FieldChange("entity", str(data["entity"]), "/attributes/hours", 54)]
    assert {link.source_ref_id for link in links} == {str(data["ref_plan"]), str(data["ref_rpd"])}
    assert touched_document_ids(snap, approved) == {data["doc_plan"], data["doc_rpd"]}
    assert {ref.document_id for ref in refs} == {str(data["doc_plan"]), str(data["doc_rpd"])}


def test_roundtrip_proposal_to_patched_docx(snapshot):
    snap, data = snapshot
    approved = plan_approved_changes(snap, _proposal(data))
    changes, links, refs = generation_inputs(snap, approved)
    doc_plan = make_docx(_table("Дисциплина", "108 ч"))
    doc_rpd = make_docx(_paragraph("Трудоёмкость: 108 ч"))
    originals = [
        OriginalDocument(str(data["doc_plan"]), "RPD", "план.docx", doc_plan),
        OriginalDocument(str(data["doc_rpd"]), "RPD", "рпд.docx", doc_rpd),
    ]
    sources = build_generation_documents(changes, links, refs, originals)
    patched = {
        source.file_name: patch_docx_bytes(source.content, source.patches) for source in sources
    }
    assert "54 ч" in document_text(patched["план.docx"])
    assert "108 ч" not in document_text(patched["план.docx"])
    assert "54 ч" in document_text(patched["рпд.docx"])


def test_stale_evidence_against_document_bytes(snapshot):
    snap, data = snapshot
    approved = plan_approved_changes(snap, _proposal(data))
    changes, links, refs = generation_inputs(snap, approved)
    doc_plan = make_docx(_table("Дисциплина", "72 ч"))
    doc_rpd_ok = make_docx(_paragraph("Трудоёмкость: 108 ч"))
    originals = [
        OriginalDocument(str(data["doc_plan"]), "RPD", "план.docx", doc_plan),
        OriginalDocument(str(data["doc_rpd"]), "RPD", "рпд.docx", doc_rpd_ok),
    ]
    with pytest.raises(GenerationMappingError, match="устарело"):
        build_generation_documents(changes, links, refs, originals)
