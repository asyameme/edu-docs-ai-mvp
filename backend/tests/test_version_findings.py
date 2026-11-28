"""Чат и интерфейс различают сохранение графа, анализ и разрешение связи."""

import asyncio
from uuid import uuid4

from document_cases import payload
from test_source_graph import build

from app.source_graph import unresolved_findings
from app.version_findings import automatic_findings


def test_missing_analysis_is_not_reported_as_absence_of_problems():
    result = automatic_findings([], {}, uuid4(), uuid4(), "failed", "not_started")
    assert len(result) == 1
    assert result[0]["rule_code"] == "VERSION_ANALYSIS_STATUS"
    assert "неполон" in result[0]["message"]
    assert not result[0]["requires_decision"]


def test_fixed_relation_closes_only_current_finding_and_keeps_original():
    parsed = payload("RPD")
    parsed["relations"] = [{
        "src_entity": "discipline", "src_key": "Дисциплина", "dst_entity": "competency",
        "dst_key": "УК-X", "kind": "формирует",
        "source_ref": {"file": "source.docx", "paragraph_index": 9},
    }]
    snapshot, _ = build([{"document_id": str(uuid4()), "parsed": parsed}])
    _, original = unresolved_findings(snapshot, snapshot.review.review_id)
    open_items = automatic_findings(original, snapshot.metadata, snapshot.review.review_id,
                                    uuid4(), "completed", "completed")
    assert open_items[0]["effective_status"] == "needs_review"
    closed = automatic_findings(original, {}, snapshot.review.review_id,
                                uuid4(), "completed", "completed")
    assert closed[0]["effective_status"] == "resolved"
    assert original[0]["status"] == "needs_review"


def test_both_entity_and_graph_tools_disclose_incomplete_relation_data():
    from app.domain import Entity, SnapshotV2
    from worker.agent.read_adapter import PostgresChatReadPort

    identity = uuid4()
    snapshot = SnapshotV2(entities=[Entity(id=identity, kind="competency")],
                          metadata={"unresolved_relations": [{"raw": "Исходная связь"}]})
    port = PostgresChatReadPort(None)
    port.snapshot = lambda *_: snapshot
    for tool in (port.read_entity, port.read_graph_projection):
        options = {"depth": 1} if tool == port.read_graph_projection else {}
        result = asyncio.run(tool(
            program_id=uuid4(), version_id=uuid4(), entity_id=identity, **options,
        ))
        assert result.unresolved_relations == 1
        assert "не доказывает" in result.completeness_note
