"""Tests for human confirmation and non-destructive graph provenance."""
from copy import deepcopy
from uuid import uuid4
import pytest

from worker.validation.validator import Finding
from worker.validation.competency_decisions import ProposedDecision, record_confirmed_decision
from contracts.document_v2.graph import EducationalGraph, GraphNode


def finding(kind='competency_code_match_candidate'):
    return Finding(kind, 'needs_review', 'user decision required', {
        'codes': ['УНК-03', 'ПКП-УНК-03'], 'code': 'УНК-03',
        'program_id': '26/5003/1',
        'variants': [
            {'document': 'matrix.json', 'code': 'УНК-03', 'formulation': 'Одна формулировка',
             'source_ref': {'file': 'matrix.json', 'section': 'competencies'}},
            {'document': 'rpd.json', 'code': 'ПКП-УНК-03', 'formulation': 'Другая формулировка',
             'source_ref': {'file': 'rpd.json', 'section': 'competencies'}},
        ], 'requires_user_confirmation': True, 'auto_merge': False,
    })


def graph():
    from contracts.document_v2.models import SourceRef
    return EducationalGraph(nodes=[
        GraphNode(id='26/5003/1|competency|УНК-03', entity_type='competency',
                  attributes={'formulation': 'Одна формулировка'},
                  source_refs=[SourceRef(file='matrix.json', section='competencies')]),
        GraphNode(id='26/5003/1|competency|ПКП-УНК-03', entity_type='competency',
                  attributes={'formulation': 'Другая формулировка'},
                  source_refs=[SourceRef(file='rpd.json', section='competencies')]),
    ])


def test_no_graph_update_without_explicit_confirmation():
    g = graph()
    p = ProposedDecision(finding(), 'accept', 'document', variant_index=0)
    with pytest.raises(PermissionError):
        record_confirmed_decision(g, p, confirmed_by='reviewer', explicit_confirmation=False)
    assert len(g.nodes) == 2


def test_choose_original_code_with_provenance_and_no_merge():
    g = graph()
    new = record_confirmed_decision(g, ProposedDecision(finding(), 'accept', 'document', variant_index=1),
                                    confirmed_by='reviewer', explicit_confirmation=True,
                                    decision_id='confirmed-code')
    assert len(g.nodes) == 2
    assert new.nodes[0].id == '26/5003/1|competency|УНК-03'
    resolution = new.nodes[-1]
    assert resolution.attributes['selected_value'] == 'ПКП-УНК-03'
    assert [r.file for r in resolution.source_refs] == ['matrix.json', 'rpd.json']
    assert len(resolution.attributes['original_variants']) == 2
    assert resolution.attributes['status'] == 'confirmed'
    assert resolution.attributes['source_node_ids'] == [
        '26/5003/1|competency|УНК-03', '26/5003/1|competency|ПКП-УНК-03']


def test_user_can_supply_custom_formulation():
    g = graph()
    new = record_confirmed_decision(g, ProposedDecision(finding('competency_formulation_review'),
                                    'accept', 'custom', custom_value='Новая авторская формулировка'),
                                    confirmed_by='reviewer', explicit_confirmation=True)
    assert new.nodes[-1].attributes['selected_value'] == 'Новая авторская формулировка'
    assert new.nodes[-1].attributes['original_variants'][0]['formulation'] == 'Одна формулировка'


def test_user_can_reject_candidate_without_removing_sources():
    new = record_confirmed_decision(graph(), ProposedDecision(finding(), 'reject'),
                                    confirmed_by='reviewer', explicit_confirmation=True)
    assert new.nodes[-1].attributes['action'] == 'reject'
    assert new.nodes[-1].attributes['selected_value'] is None


def test_invalid_manual_choices_are_rejected():
    g = graph()
    bad = [ProposedDecision(finding(), 'accept', 'custom', custom_value='   '),
           ProposedDecision(finding(), 'accept', 'document', variant_index=8),
           ProposedDecision(finding(), 'reject', 'custom', custom_value='bad')]
    for p in bad:
        with pytest.raises(ValueError):
            record_confirmed_decision(g, p, confirmed_by='reviewer', explicit_confirmation=True)
    assert len(g.nodes) == 2


def test_rejected_candidate_is_remembered_and_not_reproposed_as_pending():
    from worker.validation.competency_decisions import (
        pending_competency_findings, resolution_for_finding)
    original = finding()
    g = record_confirmed_decision(graph(), ProposedDecision(original, 'reject'),
                                  confirmed_by='reviewer', explicit_confirmation=True)
    assert resolution_for_finding(g, original)['action'] == 'reject'
    assert pending_competency_findings(g, [original]) == []


def test_repeated_or_conflicting_decisions_require_explicit_revision():
    g = record_confirmed_decision(graph(), ProposedDecision(finding(), 'reject'),
                                  confirmed_by='reviewer', explicit_confirmation=True)
    with pytest.raises(ValueError, match='already has a confirmed decision'):
        record_confirmed_decision(g, ProposedDecision(finding(), 'accept', 'document', variant_index=0),
                                  confirmed_by='reviewer', explicit_confirmation=True)


def test_formulation_can_select_new_code_and_keep_originals():
    f = finding('competency_formulation_review')
    old = graph()
    new = record_confirmed_decision(old,
        ProposedDecision(f, 'accept', 'custom', custom_value='Согласованная формулировка',
                         selected_code='УНК-04'), confirmed_by='reviewer', explicit_confirmation=True)
    assert new.nodes[-1].attributes['selected_code'] == 'УНК-04'
    assert new.nodes[-1].attributes['selected_value'] == 'Согласованная формулировка'
    assert old.nodes[0].id == new.nodes[0].id == '26/5003/1|competency|УНК-03'


def test_formulation_decision_uses_one_key_for_review_and_missing_formulation():
    from worker.validation.competency_decisions import resolution_key
    a, b = finding('competency_formulation_review'), finding('competency_formulation_missing')
    assert resolution_key(a) == resolution_key(b)


def test_finding_rejected_if_competency_is_missing_from_graph():
    g = graph()
    g.nodes.pop()  # R-01 still claims a source in rpd.json
    with pytest.raises(ValueError, match='Expected one source competency node'):
        record_confirmed_decision(g, ProposedDecision(finding(), 'accept', 'document', variant_index=0),
                                  confirmed_by='reviewer', explicit_confirmation=True)
    assert len(g.nodes) == 1


def test_finding_rejected_if_source_document_does_not_match_graph():
    bad = finding()
    bad.evidence['variants'][1]['source_ref']['file'] = 'unrelated.json'
    with pytest.raises(ValueError, match='Expected one source competency node'):
        record_confirmed_decision(graph(), ProposedDecision(bad, 'accept', 'document', variant_index=0),
                                  confirmed_by='reviewer', explicit_confirmation=True)


def test_finding_rejected_if_program_is_different():
    bad = finding()
    bad.evidence['program_id'] = 'other/program'
    with pytest.raises(ValueError, match='Expected one source competency node'):
        record_confirmed_decision(graph(), ProposedDecision(bad, 'reject'),
                                  confirmed_by='reviewer', explicit_confirmation=True)


def test_finding_without_program_cannot_span_two_programs():
    bad = finding()
    bad.evidence.pop('program_id')
    g = graph()
    g.nodes[1].id = 'other/program|competency|ПКП-УНК-03'
    with pytest.raises(ValueError, match='different educational programs'):
        record_confirmed_decision(g, ProposedDecision(bad, 'reject'),
                                  confirmed_by='reviewer', explicit_confirmation=True)


def test_existing_source_evidence_is_unchanged_on_confirmation():
    g = graph()
    original = g.model_dump()
    updated = record_confirmed_decision(g, ProposedDecision(finding(), 'reject'),
                                        confirmed_by='reviewer', explicit_confirmation=True)
    assert g.model_dump() == original
    assert [node.model_dump() for node in updated.nodes[:2]] == original['nodes']


def test_finding_rejected_if_section_mismatches_in_same_file():
    bad = finding()
    bad.evidence['variants'][0]['source_ref']['section'] = 'unrelated-section'
    with pytest.raises(ValueError, match='Expected one source competency node'):
        record_confirmed_decision(graph(), ProposedDecision(bad, 'reject'),
                                  confirmed_by='reviewer', explicit_confirmation=True)


def test_finding_rejected_if_row_coordinates_mismatch_in_same_file():
    from contracts.document_v2.models import SourceRef
    g = graph()
    g.nodes[0].source_refs = [SourceRef(file='matrix.json', section='competencies', table=2, row=3)]
    bad = finding()
    bad.evidence['variants'][0]['source_ref'].update(table=2, row=4)
    with pytest.raises(ValueError, match='Expected one source competency node'):
        record_confirmed_decision(g, ProposedDecision(bad, 'reject'),
                                  confirmed_by='reviewer', explicit_confirmation=True)


def test_exact_source_coordinates_allow_confirmation_and_preserve_graph():
    from contracts.document_v2.models import SourceRef
    g = graph()
    g.nodes[0].source_refs = [SourceRef(file='matrix.json', section='competencies', table=2, row=3)]
    f = finding()
    f.evidence['variants'][0]['source_ref'].update(table=2, row=3)
    snapshot = g.model_dump()
    updated = record_confirmed_decision(g, ProposedDecision(f, 'accept', 'document', variant_index=0),
                                        confirmed_by='reviewer', explicit_confirmation=True)
    assert updated.nodes[-1].attributes['source_node_ids'][0] == g.nodes[0].id
    assert updated.nodes[-1].source_refs[0] == g.nodes[0].source_refs[0]
    assert g.model_dump() == snapshot


def test_source_formulation_evidence_must_match_claimed_value():
    from contracts.document_v2.graph import AttributeEvidence
    from contracts.document_v2.models import SourceRef
    g = graph()
    original_ref = SourceRef(file='matrix.json', section='competencies')
    g.nodes[0].attribute_sources['formulation'] = [
        AttributeEvidence(value='Одна формулировка', source_ref=original_ref)]
    bad = finding()
    bad.evidence['variants'][0]['formulation'] = 'Подменённая формулировка'
    with pytest.raises(ValueError, match='Expected one source competency node'):
        record_confirmed_decision(g, ProposedDecision(bad, 'reject'),
                                  confirmed_by='reviewer', explicit_confirmation=True)
