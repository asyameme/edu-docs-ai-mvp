"""Тесты конвейера «примени предложение → проверь нормы» без базы и LLM."""

from test_generation_docx_patch import _paragraph, document_text, make_docx
from test_graph_projections import _mini_program_snapshot

from worker.agent.contracts import ChangeProposal, ProposedAttributeChange
from worker.agent.post_edit import apply_proposal_in_memory, validate_snapshot
from worker.generation import OriginalDocument, build_generation_documents, patch_docx_bytes


def _hours_proposal(data, *, field="hours_total", expected=108, new=54):
    return ChangeProposal(
        kind="change_proposal",
        summary="Сократить трудоёмкость",
        changes=[
            ProposedAttributeChange(
                target_kind="entity",
                target_id=data["discipline"],
                field_path=field,
                expected_old_value=expected,
                new_value=new,
                reason="По решению учёного совета",
                source_ref_ids=[data["ref_rpd"]],
            )
        ],
    )


def test_baseline_snapshot_has_no_findings():
    snapshot, _ = _mini_program_snapshot()
    assert validate_snapshot(snapshot, include_semantic=False) == []


def test_hours_change_creates_credit_hours_finding():
    snapshot, data = _mini_program_snapshot()
    outcome = apply_proposal_in_memory(
        snapshot, _hours_proposal(data), include_semantic=False
    )
    assert outcome.status == "applied"
    assert len(outcome.applied) == 1
    findings = outcome.applied[0].findings
    assert [finding.rule_code for finding in findings] == ["credit_hours_mismatch"]
    finding = findings[0]
    assert finding.type == "credit_hours"
    assert finding.severity == "warning"
    assert finding.status == "open"
    assert "54" in finding.details["message"]
    assert outcome.touched_document_ids == {data["doc_rpd"]}
    entity = next(
        item for item in outcome.final_snapshot.entities if item.id == data["discipline"]
    )
    assert entity.attributes["hours_total"] == 54
    original = next(
        item for item in snapshot.entities if item.id == data["discipline"]
    )
    assert original.attributes["hours_total"] == 108


def test_stale_proposal_is_rejected_before_any_change():
    snapshot, data = _mini_program_snapshot()
    outcome = apply_proposal_in_memory(
        snapshot, _hours_proposal(data, expected=999), include_semantic=False
    )
    assert outcome.status == "rejected"
    assert "версия изменилась" in outcome.rejection_reason
    assert outcome.applied == ()
    assert outcome.generation is None
    assert outcome.final_snapshot is None


def test_each_change_is_validated_against_its_own_snapshot():
    snapshot, data = _mini_program_snapshot()
    proposal = ChangeProposal(
        kind="change_proposal",
        summary="Часы и ЗЕТ",
        changes=[
            _hours_proposal(data).changes[0],
            ProposedAttributeChange(
                target_kind="entity",
                target_id=data["discipline"],
                field_path="credit_units",
                expected_old_value=3,
                new_value=2,
                reason="Сократить объём",
                source_ref_ids=[data["ref_rpd"]],
            ),
        ],
    )
    outcome = apply_proposal_in_memory(snapshot, proposal, include_semantic=False)
    assert outcome.status == "applied"
    assert len(outcome.applied) == 2
    assert [finding.rule_code for finding in outcome.applied[0].findings] == [
        "credit_hours_mismatch"
    ]
    assert [finding.rule_code for finding in outcome.applied[1].findings] == [
        "credit_hours_mismatch"
    ]
    entity = next(
        item for item in outcome.final_snapshot.entities if item.id == data["discipline"]
    )
    assert entity.attributes["hours_total"] == 54
    assert entity.attributes["credit_units"] == 2


def test_generation_patches_render_unit_fallback():
    snapshot, data = _mini_program_snapshot()
    outcome = apply_proposal_in_memory(
        snapshot, _hours_proposal(data), include_semantic=False
    )
    changes, links, refs = outcome.generation
    assert changes[0].new_value == 54
    original = make_docx(_paragraph("Трудоёмкость: 108 ч"))
    sources = build_generation_documents(
        changes,
        links,
        refs,
        [OriginalDocument(str(data["doc_rpd"]), "RPD", "рпд.docx", original)],
    )
    patched = {item.file_name: patch_docx_bytes(item.content, item.patches) for item in sources}
    assert "54 ч" in document_text(patched["рпд.docx"])
    assert "108 ч" not in document_text(patched["рпд.docx"])
