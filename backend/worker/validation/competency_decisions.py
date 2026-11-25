"""Human-gated competency resolutions, independent of the application Review API.

This module does not modify source documents or change competency identifiers
inside the existing graph. A confirmed decision is recorded as an additional
provenance-bearing graph node; callers can consume the overlay explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from .validator import Finding


DecisionKind = Literal['code_match', 'formulation']
DecisionAction = Literal['accept', 'reject']
SelectionKind = Literal['document', 'custom']


@dataclass(frozen=True)
class ProposedDecision:
    finding: Finding
    action: DecisionAction
    selection: SelectionKind | None = None
    variant_index: int | None = None
    custom_value: str | None = None
    # For R-07, the reviewer may also choose an explicitly entered final code.
    selected_code: str | None = None


def _checked_choice(proposal: ProposedDecision) -> tuple[DecisionKind, str | None, list[dict[str, Any]]]:
    finding = proposal.finding
    if finding.code == 'competency_code_match_candidate':
        kind: DecisionKind = 'code_match'
        field = 'code'
    elif finding.code in {'competency_formulation_review', 'competency_formulation_missing'}:
        kind = 'formulation'
        field = 'formulation'
    else:
        raise ValueError('Unsupported competency finding')
    evidence = finding.evidence
    variants = evidence.get('variants')
    if not isinstance(variants, list) or len(variants) < 2 or not all(isinstance(v, dict) for v in variants):
        raise ValueError('Finding must contain source variants')
    for v in variants:
        if not isinstance(v.get('source_ref'), dict) or not v['source_ref'].get('file'):
            raise ValueError('Source reference required for every variant')
    if proposal.action == 'reject':
        if (proposal.selection is not None or proposal.variant_index is not None
                or proposal.custom_value is not None or proposal.selected_code is not None):
            raise ValueError('Rejected candidate must not include a selected value')
        return kind, None, variants
    if proposal.action != 'accept':
        raise ValueError('Unknown action')
    if proposal.selection == 'document':
        if not isinstance(proposal.variant_index, int) or not 0 <= proposal.variant_index < len(variants):
            raise ValueError('Choose an existing document variant')
        if proposal.custom_value is not None:
            raise ValueError('Cannot combine document choice with custom value')
        selected = variants[proposal.variant_index].get(field)
    elif proposal.selection == 'custom':
        if proposal.variant_index is not None:
            raise ValueError('Cannot combine custom value with a document choice')
        selected = proposal.custom_value
    else:
        raise ValueError('A selection is required for acceptance')
    if not isinstance(selected, str) or not selected.strip():
        raise ValueError('The selected value cannot be blank')
    if kind == 'formulation' and proposal.selected_code is not None and not proposal.selected_code.strip():
        raise ValueError('Selected code cannot be blank')
    if kind == 'code_match' and proposal.selected_code is not None:
        raise ValueError('Use selected variant/custom code for code match')
    return kind, selected.strip(), variants


def resolution_key(finding: Finding) -> str:
    """Stable identity for a competency decision, independent of pairwise evidence order.

    Program and source document identifiers are included where available. Code
    matches use both codes; formulation reviews use the shared code.
    """
    evidence = finding.evidence
    program = evidence.get('program_id')
    if finding.code == 'competency_code_match_candidate':
        codes = sorted(set(evidence.get('codes') or []))
        if len(codes) != 2 or not all(isinstance(c, str) and c.strip() for c in codes):
            raise ValueError('Code match requires two different codes')
        subject = codes
    elif finding.code in {'competency_formulation_review', 'competency_formulation_missing'}:
        subject = evidence.get('code')
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError('Formulation review requires competency code')
    else:
        raise ValueError('Unsupported competency finding')
    payload = [finding.code if finding.code == 'competency_code_match_candidate' else 'formulation', program, subject]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _evidence_fingerprint(variants: Any) -> str:
    if not isinstance(variants, list) or not all(isinstance(v, dict) for v in variants):
        raise ValueError('Finding must contain source variants')
    relevant = [{field: variant.get(field) for field in
                 ('code', 'formulation', 'document', 'source_ref')} for variant in variants]
    # Validation input order can change between runs; the source values and
    # coordinates, rather than their list positions, identify this decision.
    relevant.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _decision_instance_key(key: str, fingerprint: str) -> str:
    payload = [key, fingerprint]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def resolution_instance_key(finding: Finding) -> str:
    """Identify a decision for this exact set of source evidence."""
    key = resolution_key(finding)
    fingerprint = _evidence_fingerprint(finding.evidence.get('variants'))
    return _decision_instance_key(key, fingerprint)


def get_confirmed_resolutions(graph: Any) -> dict[str, dict[str, Any]]:
    """Read confirmed audit nodes for their exact evidence snapshot.

    Callers can explicitly consume this overlay when building a resolved view.
    Source nodes/edges remain untouched. No resolution is applied automatically.
    """
    result: dict[str, dict[str, Any]] = {}
    for node in graph.nodes:
        if node.entity_type != 'competency_resolution':
            continue
        attrs = node.attributes
        if attrs.get('status') != 'confirmed':
            continue
        key = attrs.get('resolution_key')
        if not isinstance(key, str) or not key:
            # Older records without a stable key cannot be safely associated.
            continue
        instance_key = attrs.get('resolution_instance_key')
        if not isinstance(instance_key, str) or not instance_key:
            # Migrate prior audit nodes in memory. Their stored variants still
            # identify the evidence on which the human decision was based.
            try:
                fingerprint = attrs.get('evidence_fingerprint')
                if not isinstance(fingerprint, str) or not fingerprint:
                    fingerprint = _evidence_fingerprint(attrs.get('original_variants'))
                instance_key = _decision_instance_key(key, fingerprint)
            except ValueError:
                continue
        if instance_key in result:
            raise ValueError(f'Conflicting confirmed decisions for resolution instance {instance_key}')
        result[instance_key] = dict(attrs)
    return result


def resolution_for_finding(graph: Any, finding: Finding) -> dict[str, Any] | None:
    """Lookup a prior accept/reject for a validation finding without mutating it."""
    return get_confirmed_resolutions(graph).get(resolution_instance_key(finding))


def pending_competency_findings(graph: Any, findings: list[Finding]) -> list[Finding]:
    """Exclude already decided findings from a user-facing pending queue."""
    decided = get_confirmed_resolutions(graph)
    return [finding for finding in findings if resolution_instance_key(finding) not in decided]


def _source_competency_node_ids(graph: Any, finding: Finding,
                                variants: list[dict[str, Any]]) -> list[str]:
    """Resolve evidence to actual competency graph nodes, without fabricating IDs.

    The graph adapter uses ``{program_id}|competency|{code}``. Evidence must
    belong to the same program and match an actual full SourceRef on that node.
    Matching only by filename would allow false provenance from another section
    or table row in the same document. Reject missing or ambiguous links.
    """
    program_id = finding.evidence.get('program_id')
    if program_id is not None and (not isinstance(program_id, str) or not program_id.strip()):
        raise ValueError('Invalid program_id in finding')
    resolved: list[str] = []
    for variant in variants:
        code = variant.get('code')
        if not isinstance(code, str) or not code.strip():
            raise ValueError('Every source variant must have a competency code')
        reference = variant['source_ref']
        from contracts.document_v2.models import SourceRef
        source_ref = SourceRef.model_validate(reference)
        source_file = source_ref.file
        matches = []
        for node in graph.nodes:
            if node.entity_type != 'competency':
                continue
            # The identifier is authoritative; attributes may omit code.
            if '|competency|' not in node.id:
                continue
            node_program, node_code = node.id.split('|competency|', 1)
            if node_code != code:
                continue
            if program_id is not None and node_program != program_id:
                continue
            if source_ref not in node.source_refs:
                continue
            # When a per-source formulation is available, do not accept a
            # different text falsely attributed to that same source location.
            formulation = variant.get('formulation')
            if isinstance(formulation, str) and formulation.strip():
                evidence = [item for item in node.attribute_sources.get('formulation', [])
                            if item.source_ref == source_ref]
                if evidence and not any(item.value == formulation for item in evidence):
                    continue
            matches.append(node.id)
        if len(matches) != 1:
            raise ValueError(
                f'Expected one source competency node for code {code!r} '
                f'from {source_file!r}, found {len(matches)}'
            )
        if matches[0] not in resolved:
            resolved.append(matches[0])
    if len({node_id.split('|competency|', 1)[0] for node_id in resolved}) != 1:
        raise ValueError('Source variants belong to different educational programs')
    return resolved


def record_confirmed_decision(graph: Any, proposal: ProposedDecision, *,
                              confirmed_by: str, explicit_confirmation: bool,
                              decision_id: str | None = None, timestamp: str | None = None) -> Any:
    """Add a user-confirmed audit node to a deep copy, preserving source records.

    This does not change original graph entities, nor automatically decide for a
    user. A presentation/API layer must explicitly call this after confirmation.
    """
    if explicit_confirmation is not True or not isinstance(confirmed_by, str) or not confirmed_by.strip():
        raise PermissionError('Explicit user confirmation and actor are required')
    kind, value, variants = _checked_choice(proposal)
    from contracts.document_v2.graph import GraphNode
    from contracts.document_v2.models import SourceRef
    if not hasattr(graph, 'model_copy') or not hasattr(graph, 'add_node'):
        raise TypeError('Expected EducationalGraph')
    # Validate that every claimed document variant is present in this graph.
    source_node_ids = _source_competency_node_ids(graph, proposal.finding, variants)
    key = resolution_key(proposal.finding)
    fingerprint = _evidence_fingerprint(variants)
    instance_key = _decision_instance_key(key, fingerprint)
    if instance_key in get_confirmed_resolutions(graph):
        raise ValueError('This evidence already has a confirmed decision; explicit revision workflow required')
    source_refs = list(dict.fromkeys(SourceRef.model_validate(v['source_ref']).model_dump_json() for v in variants))
    new_graph = graph.model_copy(deep=True)
    identifier = decision_id or str(uuid4())
    if any(n.id == f'competency-resolution:{identifier}' for n in new_graph.nodes):
        raise ValueError('Decision ID already exists')
    attrs = {
        'rule': 'R-01' if kind == 'code_match' else 'R-07',
        'resolution_key': key,
        'resolution_instance_key': instance_key,
        'evidence_fingerprint': fingerprint,
        'finding_code': proposal.finding.code,
        'subject_codes': list(proposal.finding.evidence.get('codes') or
                              [proposal.finding.evidence.get('code')]),
        'action': proposal.action,
        'status': 'confirmed',
        'selected_value': value,
        'selected_code': ((proposal.selected_code or proposal.finding.evidence.get('code'))
                          if kind == 'formulation' and proposal.action == 'accept' else
                          value if kind == 'code_match' and proposal.action == 'accept' else None),
        'selection': proposal.selection,
        'original_variants': [dict(v) for v in variants],
        'confirmed_by': confirmed_by.strip(),
        'confirmed_at': timestamp or datetime.now(timezone.utc).isoformat(),
        'program_id': proposal.finding.evidence.get('program_id'),
        'source_node_ids': source_node_ids,
    }
    new_graph.add_node(GraphNode(id=f'competency-resolution:{identifier}',
                                 entity_type='competency_resolution', attributes=attrs,
                                 decision_basis_refs=[SourceRef.model_validate(json.loads(x)) for x in source_refs]))
    return new_graph
