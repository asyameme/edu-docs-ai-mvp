"""Поведенческие тесты агента без LLM: FunctionModel поверх фейкового порта.

TestModel не подходит: он генерирует аргументы вида ``entity_id='a'`` и падает
на валидации UUID. Scripted FunctionModel вызывает инструменты осмысленно.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.instrumented import InstrumentedModel
from pydantic_ai.models.test import TestModel

from app.domain import Entity, SourceRefV2
from worker.agent import read_port as read_port_module
from worker.agent.chat_agent import INSTRUCTIONS, MODEL_SETTINGS, create_chat_agent
from worker.agent.contracts import (
    Answer,
    ChangeProposal,
    ChatDeps,
    EntityReadResult,
    EntitySearchHit,
    EntitySearchResult,
    GraphProjection,
    SourceFragment,
)
from worker.agent.llm_journal import journalled

ENTITY_ID = uuid4()
REF_ID = uuid4()


class FakeReadPort:
    """Минимальный порт чтения: одна дисциплина, без замечаний."""

    def __init__(self):
        self.calls: list[str] = []

    @staticmethod
    def _ref() -> SourceRefV2:
        return SourceRefV2(id=uuid4(), document_id=uuid4(), locator='{"paragraph_index": 1}')

    @staticmethod
    def _discipline(ref: SourceRefV2) -> Entity:
        return Entity(
            id=uuid4(),
            kind="discipline",
            attributes={"name": "Технологии MLOps", "hours_total": 108, "credit_units": 3},
            source_ref_ids=[ref.id],
        )

    async def search_entities(
        self, *, program_id, version_id, query, entity_kinds, limit
    ) -> EntitySearchResult:
        self.calls.append("search_entities")
        ref = self._ref()
        hit = EntitySearchHit(
            entity=self._discipline(ref), summary="Дисциплина", source_refs=[ref]
        )
        return EntitySearchResult(hits=[hit])

    async def read_entity(self, *, program_id, version_id, entity_id, query) -> EntityReadResult:
        self.calls.append("read_entity")
        ref = self._ref()
        return EntityReadResult(entity=self._discipline(ref), relations=[], source_refs=[ref])

    async def read_source_fragment(
        self, *, program_id, version_id, source_ref_id, context_paragraphs
    ) -> SourceFragment:
        self.calls.append("read_source_fragment")
        ref = SourceRefV2(
            id=UUID(str(source_ref_id)), document_id=uuid4(), locator='{"paragraph_index": 1}'
        )
        fragment = SourceFragment(
            source_ref=ref, document_name="РПД_MLOps.docx", text="Трудоёмкость: 108 ч"
        )
        return fragment

    async def read_graph_projection(self, *, program_id, version_id, entity_id, depth):
        self.calls.append("read_graph_projection")
        return GraphProjection()

    async def get_validation_findings(self, *, program_id, version_id, limit):
        self.calls.append("get_validation_findings")
        return []


@pytest.fixture
def fake_port(monkeypatch):
    port = FakeReadPort()
    monkeypatch.setattr(read_port_module, "_worker_read_port", port)
    return port


def _tools_seen(messages: list[ModelMessage]) -> bool:
    return any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)


def _script(
    first_calls: list[ModelResponse], final: ToolCallPart
):  # noqa: ANN202 — фабрика функций для FunctionModel
    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if _tools_seen(messages):
            return ModelResponse(parts=[final])
        return ModelResponse(parts=first_calls)

    return run


def test_all_read_tools_are_registered_and_output_is_union(fake_port):
    agent = create_chat_agent(
        FunctionModel(
            _script(
                [
                    ToolCallPart("search_entities", {"query": "MLOps"}),
                    ToolCallPart("read_entity", {"entity_id": str(ENTITY_ID)}),
                    ToolCallPart("read_source_fragment", {"source_ref_id": str(REF_ID)}),
                    ToolCallPart("read_graph_projection", {"entity_id": str(ENTITY_ID)}),
                    ToolCallPart("get_validation_findings", {}),
                ],
                ToolCallPart(
                    "final_result_Answer",
                    {"kind": "answer", "message": "Трудоёмкость 108 ч", "sources": []},
                ),
            )
        )
    )
    result = asyncio.run(
        agent.run(
            "Покажи дисциплины программы",
            deps=ChatDeps(program_id=uuid4(), version_id=uuid4()),
        )
    )
    assert isinstance(result.output, Answer)
    assert result.output.message == "Трудоёмкость 108 ч"
    assert set(fake_port.calls) == {
        "search_entities",
        "read_entity",
        "read_source_fragment",
        "read_graph_projection",
        "get_validation_findings",
    }


def test_scripted_change_proposal_passes_union_validation(fake_port):
    agent = create_chat_agent(
        FunctionModel(
            _script(
                [ToolCallPart("read_entity", {"entity_id": str(ENTITY_ID)})],
                ToolCallPart(
                    "final_result_ChangeProposal",
                    {
                        "kind": "change_proposal",
                        "summary": "Сократить трудоёмкость",
                        "changes": [
                            {
                                "target_kind": "entity",
                                "target_id": str(ENTITY_ID),
                                "field_path": "hours_total",
                                "expected_old_value": 108,
                                "new_value": 54,
                                "reason": "По решению учёного совета",
                                "source_ref_ids": [str(REF_ID)],
                            }
                        ],
                        "unresolved_questions": [],
                    },
                ),
            )
        )
    )
    result = asyncio.run(
        agent.run(
            "Сократи трудоёмкость MLOps",
            deps=ChatDeps(program_id=uuid4(), version_id=uuid4()),
        )
    )
    assert isinstance(result.output, ChangeProposal)
    assert result.output.changes[0].new_value == 54
    assert fake_port.calls == ["read_entity"]


def test_model_settings_disable_qwen_thinking():
    assert MODEL_SETTINGS["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_instructions_cover_key_behaviour():
    for fragment in (
        "Не придумывай",
        "Clarification",
        "ChangeProposal",
        "unresolved_questions",
        "Не повторяй прежнее",
        "get_validation_findings",
        "Не утверждай, что данные изменены",
    ):
        assert fragment in INSTRUCTIONS


def test_journal_wraps_model_once_and_respects_switch(monkeypatch):
    model = TestModel()
    wrapped = journalled(model)
    assert isinstance(wrapped, InstrumentedModel)
    assert journalled(wrapped) is wrapped
    monkeypatch.setenv("CHAT_LLM_JOURNAL", "0")
    plain = TestModel()
    assert journalled(plain) is plain
