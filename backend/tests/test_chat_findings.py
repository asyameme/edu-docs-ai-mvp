"""Обзор замечаний не теряет записи и остаётся полезным при остановке агента."""

from uuid import uuid4

from app.chat_findings import build_findings_page, findings_limit_answer


def issue(message="Различаются часы", **extra):
    return {
        "id": str(uuid4()),
        "rule_code": "R-02",
        "type": "hours_mismatch",
        "severity": "warning",
        "status": "open",
        "message": message,
        "source_document_ids": [],
        "source_ref_ids": [],
        **extra,
    }


def page(rows, **kwargs):
    return build_findings_page(rows, version_id=uuid4(), documents={}, references={}, **kwargs)


def test_summary_groups_before_paginating_more_than_fifty_records():
    rows = [issue("Повтор") for _ in range(60)] + [issue("Другая проблема")]
    first = page(rows, limit=1)
    assert first.total_groups == 2
    assert first.total_records == 61
    assert len(first.groups) == 1 and first.has_more
    second = build_findings_page(
        rows,
        version_id=first.version_id,
        documents={},
        references={},
        limit=1,
        after=first.next_cursor,
    )
    assert not second.has_more
    assert {g.title for g in first.groups + second.groups} == {"Повтор", "Другая проблема"}
    assert sum(g.record_count for g in first.groups + second.groups) == 61


def test_attention_filter_happens_before_pagination():
    result = page(
        [issue(status="resolved") for _ in range(60)] + [issue("Осталась")],
        status="attention",
        limit=1,
    )
    assert result.total_groups == 1
    assert result.groups[0].title == "Осталась"
    assert result.total_records == 1


def test_summary_keeps_names_message_and_mixed_statuses():
    doc, ref = uuid4(), uuid4()
    rows = [issue(source_ref_ids=[str(ref)]), issue(status="accepted")]
    result = build_findings_page(
        rows,
        version_id=uuid4(),
        documents={str(doc): "Учебный план.docx"},
        references={str(ref): str(doc)},
    )
    group = result.groups[0]
    assert group.documents[0].name == "Учебный план.docx"
    assert group.status_counts == {"open": 1, "accepted": 1}
    assert "Различаются часы" in group.message


def test_payload_budget_has_explicit_continuation():
    result = page([issue(str(i) + "я" * 3000) for i in range(50)], limit=20)
    assert len(result.model_dump_json().encode()) < 60_000
    assert result.has_more and result.next_cursor
    assert result.truncated


def test_limit_answer_is_a_partial_read_only_summary():
    result = page([issue("В РПД 154 часа, в плане 144 часа")])
    answer = findings_limit_answer([result], "tools")
    assert answer["kind"] == "answer"
    assert "154" in answer["message"] and "144" in answer["message"]
    assert "не завершён" in answer["message"]
    assert "количества действий" in answer["message"]
    assert answer["sources"] == []
    assert "изменены" not in answer["message"]


def test_no_summary_does_not_invent_findings():
    answer = findings_limit_answer([], "tokens")
    assert "собрать" in answer["message"]
    assert "токен" in answer["message"]


def test_cursor_cannot_silently_skip_changed_scope():
    import pytest

    result = page([issue("А"), issue("Б")], limit=1)
    with pytest.raises(ValueError, match="курсор"):
        page([issue("В")], after=result.next_cursor)


def test_progress_caches_only_successfully_returned_summary(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import app.chat_progress as progress

    state = progress.ChatProgressState()
    monkeypatch.setattr(progress, "_state", lambda: state)
    expected = page([issue()])

    async def handler(args):
        return expected.model_dump(mode="json")

    result = asyncio.run(
        progress.ChatProgressCapability().wrap_tool_execute(
            None,
            call=SimpleNamespace(tool_call_id="summary"),
            tool_def=SimpleNamespace(name="get_validation_summary"),
            args={},
            handler=handler,
        )
    )
    assert result == expected.model_dump(mode="json")
    assert state.findings_pages == [expected]
    assert "findings_pages" not in state.snapshot()


def test_r11_group_titles_match_existing_frontend():
    import re
    from pathlib import Path

    from app.chat_findings import R11_TITLES

    source = Path("/workspace/frontend/src/diagnostics.ts")
    if not source.exists():
        source = Path(__file__).parents[2] / "frontend/src/diagnostics.ts"
    if not source.exists():
        import pytest

        pytest.skip("Проверка соответствия UI требует исходники frontend")
    section = source.read_text().split("const alignmentTitles:")[1].split("};")[0]
    assert dict(re.findall(r'  (\w+): "([^"]+)",', section)) == R11_TITLES


def test_actual_agent_limit_returns_cached_findings(monkeypatch):
    import asyncio
    from datetime import UTC, datetime

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from test_agent_behavior import FakeReadPort

    import app.chat_progress as progress
    import app.chat_workflow as module
    from worker.agent import read_port
    from worker.agent.chat_agent import create_chat_agent

    expected = page([issue("В РПД 154 часа, в плане 144 часа")])

    class Port(FakeReadPort):
        async def get_validation_summary(self, **kwargs):
            return expected

    count = 0

    def model(messages, info):
        nonlocal count
        count += 1
        if count == 1:
            return ModelResponse(parts=[ToolCallPart("get_validation_summary", {})])
        return ModelResponse(
            parts=[
                ToolCallPart("read_source_fragment", {"source_ref_id": str(uuid4())})
                for _ in range(3)
            ]
        )

    agent = create_chat_agent(FunctionModel(model))
    instance = module.ChatWorkflow()
    saved = []

    async def activity(name, arg, **kwargs):
        if name == "chat_save_result_v1":
            saved.append(arg)

    monkeypatch.setattr(read_port, "_worker_read_port", Port())
    monkeypatch.setattr(module, "chat_agent", agent)
    monkeypatch.setattr(module.workflow.logger, "warning", lambda *args: None)
    monkeypatch.setattr(progress, "_state", lambda: instance.chat_progress)
    monkeypatch.setattr(module.workflow, "patched", lambda _: True)
    monkeypatch.setattr(module.workflow, "now", lambda: datetime.now(UTC))
    monkeypatch.setattr(module.workflow, "execute_activity", activity)
    result = asyncio.run(
        instance.run(
            {
                "turn": str(uuid4()),
                "program_id": uuid4(),
                "version_id": expected.version_id,
                "text": "Покажи проблемы",
            }
        )
    )
    assert "154" in result["message"] and "не завершён" in result["message"]
    assert saved[0]["result"] == result
    assert "154" in saved[0]["conversation"]
    assert instance.chat_progress.findings_pages
