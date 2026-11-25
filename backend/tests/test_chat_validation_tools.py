"""Проверки backend-инструментов чата для существующего review API."""

import asyncio
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import app.chat_validation as chat_validation
from app.domain import Entity, SnapshotV2, SourceRefV2
from app.errors import DomainError
from app.repository import Repository
from app.review_contract import CheckResult
from app.review_repository import ReviewRepository
from worker.agent.chat_agent import create_chat_agent
from worker.agent.contracts import Answer, ChatDeps


def review_snapshot(review_id, *, graph_ready=True):
    return SnapshotV2(
        review={
            "review_id": review_id,
            "import_job_id": uuid4(),
            "analysis_version_id": uuid4(),
            "graph_ready": graph_ready,
        }
    ).model_dump(mode="json")


def completed_check_result(
    coverage_status="passed", *, include_issue=False, reason="Данные проверены"
):
    issues = []
    if include_issue:
        issues.append(
            {
                "id": str(uuid4()),
                "rule_code": "R-1",
                "rule_revision": "r1",
                "type": "hours_sum_mismatch",
                "severity": "warning",
                "status": "open",
                "message": "Сумма часов отличается от итога",
                "source_ref_ids": [],
                "source_document_ids": [],
                "requires_decision": False,
                "variants": [],
            }
        )
    return CheckResult.model_validate(
        {
            "schema_version": 1,
            "ruleset": {
                "id": "test-rules",
                "revision": "r1",
                "config": {},
                "required_rules": ["R-1"],
                "required_document_types": ["rpd"],
            },
            "coverage": [
                {
                    "rule_code": "R-1",
                    "rule_revision": "r1",
                    "status": "failed" if include_issue else coverage_status,
                    "reason": reason,
                }
            ],
            "issues": issues,
        }
    ).model_dump(mode="json")


def test_request_check_uses_existing_review_job_for_pinned_version(monkeypatch):
    program_id, version_id, review_id, job_id = (uuid4() for _ in range(4))
    row = {
        "parameters": {"purpose": "check", "review_id": str(review_id)},
        "input_version_id": version_id,
        "output_version_id": None,
        "report": {},
    }
    calls = {}

    monkeypatch.setattr(
        Repository,
        "version",
        lambda self, program, version: {
            "id": version,
            "snapshot": review_snapshot(review_id),
        },
    )
    monkeypatch.setattr(
        ReviewRepository,
        "create",
        lambda self, program, key, body, **kwargs: (
            calls.update(program=program, key=key, body=body, kwargs=kwargs) or job_id
        ),
    )
    monkeypatch.setattr(ReviewRepository, "job", lambda self, program, job: row)

    result = chat_validation.ChatValidationTools(object()).request_check(
        program_id, version_id, "tool-call-1"
    )

    assert calls["program"] == program_id
    assert calls["key"] == (
        f"chat-agent-check:{program_id}:{version_id}:{sha256(b'tool-call-1').hexdigest()}"
    )
    assert calls["body"].version_id == version_id
    assert calls["kwargs"] == {
        "purpose": "check",
        "review_id": review_id,
        "allow_historical": True,
    }
    assert result.check_job_id == job_id
    assert result.version_id == version_id
    assert result.status == "pending"


def test_check_request_id_is_stable_within_run_and_unique_between_runs():
    first = chat_validation._stable_check_request_id("run-1", "call_0")
    retried = chat_validation._stable_check_request_id("run-1", "call_0")
    next_run = chat_validation._stable_check_request_id("run-2", "call_0")

    assert first == retried
    assert first != next_run


def test_request_check_rejects_version_without_ready_graph(monkeypatch):
    program_id, version_id, review_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        Repository,
        "version",
        lambda self, program, version: {
            "id": version,
            "snapshot": review_snapshot(review_id, graph_ready=False),
        },
    )
    monkeypatch.setattr(
        ReviewRepository,
        "create",
        lambda *args, **kwargs: pytest.fail("Проверка не должна создаваться"),
    )

    with pytest.raises(DomainError) as error:
        chat_validation.ChatValidationTools(object()).request_check(
            program_id, version_id, "tool-call-1"
        )

    assert error.value.status == 409


def _manual_check_fixture(monkeypatch):
    program_id, version_id, review_id = uuid4(), uuid4(), uuid4()
    rpd_id, catalogue_id = uuid4(), uuid4()
    rpd_ref_id, catalogue_ref_id = uuid4(), uuid4()
    snapshot = SnapshotV2(
        document_ids=[rpd_id, catalogue_id],
        source_refs=[
            SourceRefV2(id=rpd_ref_id, document_id=rpd_id, locator="title"),
            SourceRefV2(id=catalogue_ref_id, document_id=catalogue_id, locator="table:1;row:2"),
        ],
        entities=[
            Entity(
                id=uuid4(),
                kind="competency",
                attributes={"code": "ПКП-ППК-ДА3", "formulation": "Анализирует данные"},
                source_ref_ids=[catalogue_ref_id],
            )
        ],
        review={
            "review_id": review_id,
            "import_job_id": uuid4(),
            "analysis_version_id": uuid4(),
            "graph_ready": True,
        },
    )
    documents = [
        {
            "document_id": str(rpd_id),
            "parsed": {"meta": {"doc_type": "RPD", "file_name": "Глубокое обучение.docx"}},
        },
        {
            "document_id": str(catalogue_id),
            "parsed": {"meta": {"doc_type": "CKMV", "file_name": "ЦКМВ.docx"}},
        },
    ]
    monkeypatch.setattr(
        Repository,
        "version",
        lambda self, program, version: {
            "id": version,
            "snapshot": snapshot.model_dump(mode="json"),
        },
    )
    return program_id, version_id, review_id, rpd_id, documents


def test_list_manual_check_targets_matches_document_and_competency(monkeypatch):
    program_id, version_id, review_id, rpd_id, documents = _manual_check_fixture(monkeypatch)
    monkeypatch.setattr(
        ReviewRepository,
        "job",
        lambda self, program, job: {
            "parameters": {"purpose": "prepare", "input": {"documents": documents}}
        },
    )

    tools = chat_validation.ChatValidationTools(object())
    rpd = tools.list_targets(program_id, version_id, "R-11", "глубокое обучение")
    competency = tools.list_targets(program_id, version_id, "R-09", "ПКП-ППК-ДА3")

    assert [(item.document_id, item.label) for item in rpd.targets] == [
        (rpd_id, "Глубокое обучение.docx")
    ]
    assert [(item.competency_code, item.label) for item in competency.targets] == [
        ("ПКП-ППК-ДА3", "ПКП-ППК-ДА3 — Анализирует данные")
    ]


@pytest.mark.parametrize("rule_code", ["R-09", "R-11"])
def test_request_check_creates_only_the_selected_manual_scope(monkeypatch, rule_code):
    program_id, version_id, review_id, rpd_id, documents = _manual_check_fixture(monkeypatch)
    job_id = uuid4()
    target_params = (
        {"manual_rule_codes": ["R-09"], "manual_competency_code": "ПКП-ППК-ДА3"}
        if rule_code == "R-09"
        else {"manual_rule_codes": ["R-11"], "manual_document_ids": [str(rpd_id)]}
    )
    calls = {}

    def read_job(self, program, identity):
        if identity == review_id:
            return {"parameters": {"purpose": "prepare", "input": {"documents": documents}}}
        return {
            "parameters": {
                "purpose": "check",
                "review_id": str(review_id),
                **target_params,
            },
            "input_version_id": version_id,
            "output_version_id": None,
            "report": {"outcome": "pending"},
        }

    monkeypatch.setattr(ReviewRepository, "job", read_job)
    monkeypatch.setattr(
        ReviewRepository,
        "create",
        lambda self, program, key, body, **kwargs: (
            calls.update(key=key, body=body, kwargs=kwargs) or job_id
        ),
    )

    kwargs = (
        {"rule_code": "R-09", "competency_code": "ПКП-ППК-ДА3"}
        if rule_code == "R-09"
        else {"rule_code": "R-11", "document_id": rpd_id}
    )
    result = chat_validation.ChatValidationTools(object()).request_check(
        program_id, version_id, "scoped-tool-call", **kwargs
    )

    assert calls["body"].version_id == version_id
    assert calls["body"].rule_codes == [rule_code]
    if rule_code == "R-09":
        assert calls["body"].competency_code == "ПКП-ППК-ДА3"
        assert calls["body"].document_ids is None
        assert result.scope_rule_code == "R-09"
        assert result.competency_code == "ПКП-ППК-ДА3"
    else:
        assert calls["body"].document_ids == [rpd_id]
        assert calls["body"].competency_code is None
        assert result.scope_rule_code == "R-11"
        assert result.document_id == rpd_id


def test_request_check_rejects_target_outside_available_manual_scope(monkeypatch):
    program_id, version_id, review_id, _, documents = _manual_check_fixture(monkeypatch)
    monkeypatch.setattr(
        ReviewRepository,
        "job",
        lambda self, program, job: {
            "parameters": {"purpose": "prepare", "input": {"documents": documents}}
        },
    )
    monkeypatch.setattr(
        ReviewRepository,
        "create",
        lambda *args, **kwargs: pytest.fail("Неизвестную цель нельзя запускать"),
    )

    with pytest.raises(DomainError, match="не найдена"):
        chat_validation.ChatValidationTools(object()).request_check(
            program_id,
            version_id,
            "bad-target-tool-call",
            rule_code="R-09",
            competency_code="UNKNOWN",
        )


def test_read_check_returns_bounded_result_for_exact_version(monkeypatch):
    program_id, version_id, review_id, job_id = (uuid4() for _ in range(4))
    monkeypatch.setattr(
        Repository,
        "version",
        lambda self, program, version: {
            "id": version,
            "snapshot": review_snapshot(review_id),
        },
    )
    monkeypatch.setattr(
        ReviewRepository,
        "job",
        lambda self, program, job: {
            "parameters": {"purpose": "check", "review_id": str(review_id)},
            "input_version_id": version_id,
            "output_version_id": None,
            "report": {"outcome": "completed", "result": completed_check_result()},
        },
    )

    result = chat_validation.ChatValidationTools(object()).read_check(
        program_id, version_id, job_id
    )

    assert result.status == "completed"
    assert result.input_version_id == version_id
    assert result.output_version_id is None
    assert result.coverage[0].rule_code == "R-1"
    assert result.coverage[0].status == "passed"
    assert result.total_issues == 0
    assert result.all_rules_passed is True


@pytest.mark.parametrize(
    ("coverage_status", "include_issue", "expected_pass"),
    [("skipped", False, False), ("not_applicable", False, False), ("passed", True, False)],
)
def test_completed_check_is_not_assumed_passed_from_status_alone(
    monkeypatch, coverage_status, include_issue, expected_pass
):
    program_id, version_id, review_id, job_id = (uuid4() for _ in range(4))
    monkeypatch.setattr(
        Repository,
        "version",
        lambda self, program, version: {
            "id": version,
            "snapshot": review_snapshot(review_id),
        },
    )
    monkeypatch.setattr(
        ReviewRepository,
        "job",
        lambda self, program, job: {
            "parameters": {"purpose": "check", "review_id": str(review_id)},
            "input_version_id": version_id,
            "output_version_id": None,
            "report": {
                "outcome": "completed",
                "result": completed_check_result(coverage_status, include_issue=include_issue),
            },
        },
    )

    result = chat_validation.ChatValidationTools(object()).read_check(
        program_id, version_id, job_id
    )

    assert result.status == "completed"
    assert result.all_rules_passed is expected_pass


def test_read_check_rejects_another_version(monkeypatch):
    program_id, version_id, review_id, job_id = (uuid4() for _ in range(4))
    other_version_id = uuid4()
    monkeypatch.setattr(
        Repository,
        "version",
        lambda self, program, version: {
            "id": version,
            "snapshot": review_snapshot(review_id),
        },
    )
    monkeypatch.setattr(
        ReviewRepository,
        "job",
        lambda self, program, job: {
            "parameters": {"purpose": "check", "review_id": str(review_id)},
            "input_version_id": other_version_id,
            "output_version_id": other_version_id,
            "report": {"outcome": "completed", "result": completed_check_result()},
        },
    )

    with pytest.raises(DomainError) as error:
        chat_validation.ChatValidationTools(object()).read_check(program_id, version_id, job_id)

    assert error.value.status == 404


def test_registered_backend_tool_is_callable_by_existing_chat_agent(monkeypatch):
    program_id, version_id, job_id = uuid4(), uuid4(), uuid4()

    class Tools:
        calls = []

        def request_check(self, program, version, request_id, **scope):
            self.calls.append(("request", program, version, request_id, scope))
            return chat_validation.ChatCheckResult(
                check_job_id=job_id,
                version_id=version,
                input_version_id=version,
                status="pending",
                coverage=[],
                issues=[],
                total_coverage=0,
                total_issues=0,
                truncated=False,
                all_rules_passed=None,
            )

        def read_check(self, program, version, check_job):
            self.calls.append(("read", program, version, check_job))
            return chat_validation.ChatCheckResult(
                check_job_id=check_job,
                version_id=version,
                input_version_id=version,
                status="completed",
                coverage=[
                    chat_validation.ChatCheckCoverage(
                        rule_code="R-1", status="passed", reason="Проверено"
                    )
                ],
                issues=[],
                total_coverage=1,
                total_issues=0,
                truncated=False,
                all_rules_passed=True,
            )

    tools = Tools()
    monkeypatch.setattr(chat_validation, "_chat_validation_tools", tools)

    def respond(messages: list, info: AgentInfo) -> ModelResponse:
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("request_program_check", {})])
        if returns[-1].tool_name == "request_program_check":
            return ModelResponse(
                parts=[ToolCallPart("read_program_check", {"check_job_id": str(job_id)})]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Answer",
                    {"kind": "answer", "message": "Проверка завершена", "sources": []},
                )
            ]
        )

    agent = create_chat_agent(FunctionModel(respond))
    chat_validation.register_chat_validation_tools(agent)
    result = asyncio.run(
        agent.run(
            "Проверь программу",
            deps=ChatDeps(program_id=program_id, version_id=version_id),
        )
    )
    second_result = asyncio.run(
        agent.run(
            "Проверь программу",
            deps=ChatDeps(program_id=program_id, version_id=version_id),
        )
    )

    assert isinstance(result.output, Answer)
    assert isinstance(second_result.output, Answer)
    assert result.output.message == "Проверка завершена"
    assert tools.calls[0][:3] == ("request", program_id, version_id)
    assert tools.calls[0][3]
    assert tools.calls[1] == ("read", program_id, version_id, job_id)
    assert tools.calls[2][:3] == ("request", program_id, version_id)
    assert tools.calls[2][3] != tools.calls[0][3]


def test_large_multibyte_check_result_is_bounded_and_marked_truncated(monkeypatch):
    program_id, version_id, review_id, job_id = (uuid4() for _ in range(4))
    coverage_count = 80
    raw_result = {
        "schema_version": 1,
        "ruleset": {
            "id": "large-rules",
            "revision": "r1",
            "config": {},
            "required_rules": [f"R-{index}" for index in range(coverage_count)],
            "required_document_types": ["rpd"],
        },
        "coverage": [
            {
                "rule_code": f"R-{index}",
                "rule_revision": "r1",
                "status": "passed",
                "reason": "проверено" * 600,
            }
            for index in range(coverage_count)
        ],
        "issues": [],
    }
    monkeypatch.setattr(
        Repository,
        "version",
        lambda self, program, version: {
            "id": version,
            "snapshot": review_snapshot(review_id),
        },
    )
    monkeypatch.setattr(
        ReviewRepository,
        "job",
        lambda self, program, job: {
            "parameters": {"purpose": "check", "review_id": str(review_id)},
            "input_version_id": version_id,
            "output_version_id": None,
            "report": {"outcome": "completed", "result": raw_result},
        },
    )

    result = chat_validation.ChatValidationTools(object()).read_check(
        program_id, version_id, job_id
    )

    assert len(chat_validation.to_json(result)) <= chat_validation.MAX_CHAT_TOOL_PAYLOAD_BYTES
    assert result.total_coverage == coverage_count
    assert result.truncated is True
    assert result.all_rules_passed is False
    assert len(result.coverage) < result.total_coverage
