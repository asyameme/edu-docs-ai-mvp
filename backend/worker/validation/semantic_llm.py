"""Общий адаптер вызова LLM для семантических проверок валидатора."""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4


SemanticEvaluator = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]

logger = logging.getLogger(__name__)


class SemanticEvaluationUnavailable(RuntimeError):
    """Семантическая модель не настроена или вернула непригодный ответ."""


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped.strip("`").lstrip()
    if stripped[:4].lower() == "json":
        stripped = stripped[4:].lstrip()
    return stripped


def _trace(event: str, run_id: str, **fields: Any) -> None:
    """Пишет воспроизводимый запрос/ответ без ключей доступа провайдера."""
    record = {
        "event": event,
        "run_id": run_id,
        "timestamp": datetime.now(UTC).isoformat(),
        **fields,
    }
    logger.info("semantic_llm_trace %s", json.dumps(record, ensure_ascii=False, default=str))


def evaluate_semantic_payload(
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    system_prompt: str,
    evaluator: SemanticEvaluator | None = None,
) -> Mapping[str, Any]:
    """Вызывает внедрённый оценщик или LiteLLM и возвращает разобранный JSON-объект."""
    if evaluator is not None:
        return evaluator(payload, config)

    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise SemanticEvaluationUnavailable(
            "Не установлен python-dotenv; установите зависимости backend."
        ) from exc

    project_root = Path(__file__).resolve().parents[3]
    load_dotenv(project_root / ".env", override=False)
    model = os.getenv(str(config.get("model_env", "VALIDATION_LLM_MODEL")))
    api_key = os.getenv(str(config.get("api_key_env", "VALIDATION_LLM_API_KEY")))
    api_base = os.getenv(str(config.get("api_base_env", "VALIDATION_LLM_API_BASE")))
    if not model or not api_key:
        raise SemanticEvaluationUnavailable(
            "Не настроены VALIDATION_LLM_MODEL и/или VALIDATION_LLM_API_KEY."
        )
    try:
        from litellm import completion
    except ImportError as exc:
        raise SemanticEvaluationUnavailable(
            "Не установлен LiteLLM; установите зависимости backend."
        ) from exc

    run_id = str(uuid4())
    user_prompt = json.dumps(payload, ensure_ascii=False)
    request: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": int(config.get("max_tokens", 1800)),
        "timeout": float(config.get("timeout_seconds", 60)),
    }
    if api_base:
        request["api_base"] = api_base
    if config.get("disable_thinking", True):
        # Параметр нужен Qwen3 на vLLM; для других эндпоинтов его можно отключить.
        request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

    _trace(
        "request",
        run_id,
        prompt_version=config.get("prompt_version", "unversioned"),
        model=model,
        system_prompt=system_prompt,
        input=payload,
    )
    try:
        response = completion(**request)
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise SemanticEvaluationUnavailable("LLM вернула ответ не в текстовом формате.")
    except SemanticEvaluationUnavailable:
        _trace("error", run_id, error_type="SemanticEvaluationUnavailable")
        raise
    except Exception as exc:
        # Тело ошибки провайдера не логируем: оно может содержать секреты или входные данные.
        _trace("error", run_id, error_type=type(exc).__name__)
        raise SemanticEvaluationUnavailable(
            f"Вызов LLM завершился ошибкой ({type(exc).__name__})."
        ) from exc

    try:
        parsed = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError as exc:
        _trace(
            "invalid_json_response",
            run_id,
            raw_output=content,
            error_type=type(exc).__name__,
        )
        raise SemanticEvaluationUnavailable(
            "LLM вернула ответ, который не удалось разобрать как JSON."
        ) from exc

    if not isinstance(parsed, Mapping):
        _trace("invalid_response", run_id, raw_output=content)
        raise SemanticEvaluationUnavailable("LLM вернула JSON не в виде объекта.")

    _trace("response", run_id, raw_output=content, parsed_output=parsed)
    return parsed
