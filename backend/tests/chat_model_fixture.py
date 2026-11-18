"""Локальный OpenAI-compatible ответчик для изолированной приёмки чата."""

import asyncio
import json
import re
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request

app = FastAPI()

_READ_TOOLS = {
    "search_entities",
    "read_entity",
    "read_source_fragment",
    "read_graph_projection",
    "get_validation_findings",
    "request_program_check",
    "read_program_check",
}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return ""


def _latest_turn(messages):
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index, _text(messages[index].get("content"))
    raise HTTPException(status_code=400, detail="В запросе нет сообщения пользователя")


def _latest_tool_result(messages, start):
    call_names = {}
    result = None
    for message in messages[start + 1 :]:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                call_names[call["id"]] = call["function"]["name"]
        elif message.get("role") == "tool":
            name = call_names.get(message.get("tool_call_id"), "")
            try:
                body = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=500, detail="Ответ инструмента не JSON") from error
            result = name, body
    return result


def _available_tools(payload) -> set[str]:
    return {
        item.get("function", {}).get("name", "")
        for item in payload.get("tools", [])
        if item.get("type") == "function"
    }


def _tool_response(model: str, tool_name: str, arguments: dict):
    return {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call-{uuid4().hex}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def _result_tool(available: set[str], output_name: str) -> str:
    expected = f"final_result_{output_name}"
    if expected in available:
        return expected
    candidates = sorted(name for name in available if name.startswith("final_result"))
    if len(candidates) == 1:
        return candidates[0]
    raise HTTPException(status_code=500, detail=f"Не найдена схема результата {output_name}")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completion(request: Request):
    payload = await request.json()
    messages = payload.get("messages", [])
    turn_index, user_text = _latest_turn(messages)
    previous = _latest_tool_result(messages, turn_index)
    available = _available_tools(payload)
    model = str(payload.get("model", "chat-test"))

    if "fixture-model-timeout" in user_text:
        await asyncio.sleep(5)

    if "fixture-slow" in user_text:
        await asyncio.sleep(1)

    if "fixture-model-failure" in user_text:
        raise HTTPException(status_code=401, detail="Synthetic chat model failure")

    if "проверь" in user_text.lower() and "request_program_check" in available:
        if previous is None:
            return _tool_response(model, "request_program_check", {})
        if previous[0] == "request_program_check":
            return _tool_response(
                model,
                "read_program_check",
                {"check_job_id": previous[1]["check_job_id"]},
            )
        if previous[0] == "read_program_check":
            status = previous[1]["status"]
            job_id = previous[1]["check_job_id"]
            if status == "completed" and previous[1].get("all_rules_passed") is True:
                message = f"Проверка завершена, все правила пройдены; задача {job_id}."
            elif status == "completed":
                message = (
                    "Проверка завершена, результат не подтверждает прохождение "
                    f"всех правил; задача {job_id}."
                )
            elif status == "pending":
                message = f"Проверка ещё выполняется; задача {job_id}."
            elif status == "partial":
                message = f"Проверка завершена частично; задача {job_id}."
            else:
                message = f"Проверка завершилась ошибкой; задача {job_id}."
            if previous[1].get("truncated") is True:
                message += " Показана только часть результата."
            return _tool_response(
                model,
                _result_tool(available, "Answer"),
                {"kind": "answer", "message": message, "sources": []},
            )

    if previous is None:
        if "search_entities" not in available:
            raise HTTPException(status_code=500, detail="Не зарегистрирован search_entities")
        return _tool_response(
            model,
            "search_entities",
            {"query": "Тестовая дисциплина", "limit": 5},
        )

    tool_name, tool_result = previous
    if tool_name == "search_entities":
        hits = tool_result.get("hits", [])
        if not hits:
            raise HTTPException(status_code=500, detail="Тестовая дисциплина не найдена")
        hit = hits[0]
        entity_id = hit["entity"]["id"]
        source_refs = hit.get("source_refs", [])
        if not source_refs:
            raise HTTPException(status_code=500, detail="В тестовой дисциплине нет источников")
        source_ref_id = source_refs[0]["id"]
        if any(word in user_text.lower() for word in ("сократи", "измени", "поменяй")):
            return _tool_response(model, "read_entity", {"entity_id": entity_id})
        if "текущем графе" in user_text.lower():
            return _tool_response(model, "read_entity", {"entity_id": entity_id})
        return _tool_response(
            model,
            "read_source_fragment",
            {"source_ref_id": source_ref_id, "context_paragraphs": 0},
        )

    if tool_name == "read_source_fragment":
        fragment = tool_result
        source_ref = fragment["source_ref"]
        return _tool_response(
            model,
            _result_tool(available, "Answer"),
            {
                "kind": "answer",
                "message": f"В исходном документе указано: {fragment['text']}",
                "sources": [source_ref],
            },
        )

    if tool_name == "read_entity":
        entity = tool_result["entity"]
        source_refs = tool_result.get("source_refs", [])
        if not source_refs:
            raise HTTPException(status_code=500, detail="В тестовой дисциплине нет источников")
        source_ref_id = (
            str(uuid4()) if "fixture-foreign-source" in user_text else source_refs[0]["id"]
        )
        if any(word in user_text.lower() for word in ("сократи", "измени", "поменяй")):
            new_values = re.findall(r"\d+", user_text)
            new_value = int(new_values[-1]) if new_values else 54
            return _tool_response(
                model,
                _result_tool(available, "ChangeProposal"),
                {
                    "kind": "change_proposal",
                    "summary": "Изменить трудоёмкость дисциплины",
                    "changes": [
                        {
                            "target_kind": "entity",
                            "target_id": entity["id"],
                            "field_path": "hours_total",
                            "expected_old_value": entity["attributes"]["hours_total"],
                            "new_value": new_value,
                            "reason": "Запрос пользователя в тестовом сценарии",
                            "source_ref_ids": [source_ref_id],
                        }
                    ],
                    "unresolved_questions": [],
                },
            )
        hours = entity.get("attributes", {}).get("hours_total")
        return _tool_response(
            model,
            _result_tool(available, "Answer"),
            {
                "kind": "answer",
                "message": f"В текущем графе сохранено {hours} часов.",
                "sources": [source_refs[0]],
            },
        )

    raise HTTPException(status_code=500, detail=f"Неожиданный tool: {tool_name}")
