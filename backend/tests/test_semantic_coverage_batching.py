import json

import pytest

from worker.validation import semantic_coverage as coverage

CODE = "ПКП-1"
CONFIG = {"max_prompt_tokens": 4000}


def _documents(*, topics):
    items = [
        {"discipline_name": "Анализ данных", "semester": semester, "source_ref": {
            "table": 1, "row": semester,
        }}
        for semester in (5, 6)
    ]
    return [
        coverage.CoverageDocument(
            {"competencies": [{"code": CODE, "formulation": "Анализирует данные"}]},
            "Характеристика.docx", "characteristic", None,
        ),
        coverage.CoverageDocument({
            "curriculum_items": items,
            "relations": [
                {
                    "src_entity": "curriculum_item", "src_key": item["discipline_name"],
                    "dst_entity": "competency", "dst_key": CODE, "kind": "формирует",
                    "source_ref": item["source_ref"],
                }
                for item in items
            ],
        }, "План.docx", "curriculum", None),
        coverage.CoverageDocument({
            "disciplines": [{"name": "Анализ данных", "semesters": [5, 6]}],
            "course_content": [
                {"topic": topic, "source_ref": {"table": 2, "row": index}}
                for index, topic in enumerate(topics)
            ],
        }, "РПД.docx", "rpd", None),
    ]


@pytest.fixture(autouse=True)
def deterministic_prompt_size(monkeypatch):
    monkeypatch.setattr(
        coverage, "estimate_prompt_tokens",
        lambda body, prompt, _: len(json.dumps(body, ensure_ascii=False)) + len(prompt),
    )


def test_large_coverage_checks_all_chunks_and_preserves_original_source():
    topics = [f"Тема {index} " + "аналитическое исследование " * 30 for index in range(14)]
    observed = []
    excerpts = []
    course_ids = []

    def evaluator(body, options):
        prompt = options["_system_prompt"]
        assert coverage.estimate_prompt_tokens(body, prompt, options) <= CONFIG["max_prompt_tokens"]
        if prompt == coverage.EXTRACT_PROMPT:
            for item in body["evidence"]:
                assert "source_ref" not in item
                if item["kind"] == "course_content":
                    excerpts.append(item["text"])
                    course_ids.append(item["id"])
            cited = [item["id"] for item in body["evidence"] if item["kind"] == "course_content"]
            observed.append({
                "summary": "Результаты по части материалов", "evidence_ids": cited[-1:],
            })
            if len(observed) == 2:
                observation = observed[-1]
                observation["summary"] += f" ({cited[-1]})."
                return {"summary": observation["summary"]}
            if len(observed) == 3:
                observation = observed[-1]
                observation["summary"] += f" ({cited[-2]})."
                response = dict(observation)
                observation["evidence_ids"] = cited[-1:] + cited[-2:-1]
                return response
            return observed[-1]

        assert prompt == coverage.SYNTHESIS_PROMPT
        group = body["competency_groups"][0]
        assert group["evidence"] == observed
        return {"results": [{
            "group_id": group["group_id"], "status": "gap", "reason": "Обнаружен пробел",
            "evidence_ids": course_ids[-1:],
        }]}

    checks, findings, summaries = coverage.evaluate_competency_coverage(
        _documents(topics=topics), llm_config=CONFIG, evaluator=evaluator,
    )

    assert checks == 1 and len(observed) > 1
    assert excerpts == [topic.strip() for topic in topics]
    assert [finding["code"] for finding in findings] == ["competency_coverage_semantic_gap"]
    assert {"file": "РПД.docx", "table": 2, "row": 13} in findings[0]["evidence"]["source_refs"]


def test_small_coverage_uses_one_request():
    called = []

    def evaluator(body, options):
        called.append(options["_system_prompt"])
        group = body["competency_groups"][0]
        course_rows = [row for row in group["evidence"] if row["kind"] == "course_content"]
        assert len(course_rows) == 1
        return {"results": [{
            "group_id": group["group_id"], "status": "supported", "reason": "Есть подтверждение",
            "evidence_ids": [course_rows[0]["id"]],
        }]}

    checks, findings, summaries = coverage.evaluate_competency_coverage(
        _documents(topics=["Анализ данных"]), llm_config=CONFIG, evaluator=evaluator,
    )

    assert (checks, findings) == (1, [])
    assert summaries == [{
        "competency_code": CODE, "status": "supported", "reason": "Есть подтверждение",
    }]
    assert called == [coverage.SYSTEM_PROMPT]


@pytest.mark.parametrize(
    ("topic_count", "expected_requests"),
    [(1, 3), (5, 5)],
)
def test_targeted_coverage_respects_five_request_budget(
    monkeypatch, topic_count, expected_requests,
):
    def prompt_size(body, prompt, _config):
        if prompt == coverage.SYSTEM_PROMPT:
            return 5000
        if prompt in {coverage.EXTRACT_PROMPT, coverage.EXTRACT_RETRY_PROMPT}:
            return 2000 * len(body["evidence"])
        return 2000

    monkeypatch.setattr(coverage, "estimate_prompt_tokens", prompt_size)
    calls = []

    def evaluator(body, options):
        prompt = options["_system_prompt"]
        calls.append(prompt)
        if prompt == coverage.EXTRACT_PROMPT:
            evidence_ids = [item["id"] for item in body["evidence"]]
            return {"summary": "Краткая выжимка", "evidence_ids": evidence_ids[:1]}

        group = body["competency_groups"][0]
        evidence_id = group["evidence"][0]["evidence_ids"][0]
        return {"results": [{
            "group_id": group["group_id"],
            "status": "supported",
            "reason": "Найдены подтверждающие материалы",
            "evidence_ids": [evidence_id],
        }]}

    checks, findings, summaries = coverage.evaluate_competency_coverage(
        _documents(topics=[f"Тема {index}" for index in range(topic_count)]),
        llm_config=CONFIG | {"max_requests_per_competency": 5},
        evaluator=evaluator,
        competency_code=CODE,
    )

    assert (checks, findings) == (1, [])
    assert calls.count(coverage.EXTRACT_PROMPT) == expected_requests - 1
    assert calls.count(coverage.SYNTHESIS_PROMPT) == 1
    assert len(calls) == expected_requests


def test_targeted_coverage_does_not_fan_out_past_eight_requests():
    calls = []

    def evaluator(body, options):
        calls.append(options["_system_prompt"])
        return {"summary": "Наблюдение", "evidence_ids": [body["evidence"][0]["id"]]}

    checks, findings, summaries = coverage.evaluate_competency_coverage(
        _documents(topics=[f"Тема {index} " + "содержание " * 95 for index in range(30)]),
        llm_config=CONFIG,
        evaluator=evaluator,
        competency_code=CODE,
    )

    assert checks == 1
    assert len(calls) <= 8
    assert [finding["code"] for finding in findings] == [
        "competency_coverage_request_too_large"
    ]


def test_large_coverage_rejects_citations_from_outside_chunk():
    calls = []

    def evaluator(body, options):
        calls.append(options["_system_prompt"])
        return {"summary": "Наблюдение по части", "evidence_ids": ["E9999"]}

    checks, findings, summaries = coverage.evaluate_competency_coverage(
        _documents(topics=[f"Тема {index} " + "содержание " * 95 for index in range(10)]),
        llm_config=CONFIG, evaluator=evaluator,
    )

    assert checks == 1
    assert calls == [coverage.EXTRACT_PROMPT, coverage.EXTRACT_RETRY_PROMPT]
    assert [finding["code"] for finding in findings] == ["competency_coverage_response_invalid"]


def test_invalid_observation_retries_only_that_part():
    chunks = []
    retried = []

    def evaluator(body, options):
        prompt = options["_system_prompt"]
        assert coverage.estimate_prompt_tokens(body, prompt, options) <= CONFIG["max_prompt_tokens"]
        if prompt == coverage.SYNTHESIS_PROMPT:
            group = body["competency_groups"][0]
            assert len(group["evidence"]) == len(chunks)
            return {"results": [{
                "group_id": group["group_id"], "status": "supported", "reason": "Есть покрытие",
                "evidence_ids": group["evidence"][-1]["evidence_ids"],
            }]}

        evidence_ids = [item["id"] for item in body["evidence"]]
        if prompt == coverage.EXTRACT_RETRY_PROMPT:
            retried.append(evidence_ids)
        else:
            chunks.append(evidence_ids)
            if len(chunks) == 2:
                return {"summary": "Наблюдение без идентификаторов"}
        return {"summary": "Результат по части", "evidence_ids": evidence_ids[-1:]}

    checks, findings, summaries = coverage.evaluate_competency_coverage(
        _documents(topics=[f"Тема {index} " + "содержание " * 95 for index in range(10)]),
        llm_config=CONFIG, evaluator=evaluator,
    )

    assert (checks, findings) == (1, [])
    assert len(chunks) > 2
    assert retried == [chunks[1]]


def test_invalid_conclusion_retries_only_the_conclusion():
    conclusions = []

    def evaluator(body, options):
        prompt = options["_system_prompt"]
        assert coverage.estimate_prompt_tokens(body, prompt, options) <= CONFIG["max_prompt_tokens"]
        if prompt == coverage.EXTRACT_PROMPT:
            assert not conclusions
            return {"summary": "Наблюдение", "evidence_ids": [body["evidence"][0]["id"]]}

        group = body["competency_groups"][0]
        conclusions.append((prompt, body))
        first_citation = group["evidence"][0]["evidence_ids"][0]
        cited = "E9999" if prompt == coverage.SYNTHESIS_PROMPT else first_citation
        return {"results": [{
            "group_id": group["group_id"], "status": "supported", "reason": "Есть покрытие",
            "evidence_ids": [cited],
        }]}

    checks, findings, summaries = coverage.evaluate_competency_coverage(
        _documents(topics=[f"Тема {index} " + "содержание " * 95 for index in range(10)]),
        llm_config=CONFIG, evaluator=evaluator,
    )

    assert (checks, findings) == (1, [])
    assert [prompt for prompt, _ in conclusions] == [
        coverage.SYNTHESIS_PROMPT, coverage.SYNTHESIS_RETRY_PROMPT,
    ]
    assert conclusions[0][1] == conclusions[1][1]
