"""Минимальный журнал LLM-вызовов чат-агента: промпт, вход, ответ, расход.

Модель оборачивается в ``InstrumentedModel``; спаны GenAI пишутся в журнал
приложения по одной JSON-строке на спан. Формат и место хранения не окончательны
(цикл перепроверки семантических проверок — открытый вопрос), поэтому без
отдельной таблицы: только logging, без изменений бекенда.

Журнал выключается переменной окружения ``CHAT_LLM_JOURNAL=0``. Если
opentelemetry-sdk недоступен, журнал молча отключается — агент работает как есть.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.models.instrumented import InstrumentedModel

logger = logging.getLogger("worker.agent.llm")

_provider: Any = None
_sdk_available: bool | None = None


def journal_enabled() -> bool:
    """Журнал включён по умолчанию; ``CHAT_LLM_JOURNAL=0`` его выключает."""

    return os.environ.get("CHAT_LLM_JOURNAL", "1") != "0"


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Sequence):
        return [_plain(item) for item in value]
    return str(value)


def _build_exporter() -> Any:
    from opentelemetry.sdk.trace.export import ReadableSpan, SpanExporter, SpanExportResult

    class _LoggingSpanExporter(SpanExporter):
        """Каждый завершённый спан — одна JSON-строка в журнале приложения."""

        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            for span in spans:
                record = {
                    "span": span.name,
                    "trace_id": format(span.get_span_context().trace_id, "032x"),
                    "attributes": {key: _plain(value) for key, value in span.attributes.items()},
                }
                logger.info(json.dumps(record, ensure_ascii=False, default=str))
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return _LoggingSpanExporter()


def _tracer_provider() -> Any:
    global _provider, _sdk_available
    if _provider is not None:
        return _provider
    if _sdk_available is None:
        try:
            from opentelemetry.sdk.resources import Resource  # noqa: F401
            from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: F401
        except ImportError:
            _sdk_available = False
        else:
            _sdk_available = True
    if not _sdk_available:
        return None
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "edu-chat-agent"}))
    provider.add_span_processor(SimpleSpanProcessor(_build_exporter()))
    _provider = provider
    return _provider


def journalled(model: Model) -> Model:
    """Обернуть модель журналом; повторная обёртка и выключенный журнал — no-op."""

    if isinstance(model, InstrumentedModel) or not journal_enabled():
        return model
    provider = _tracer_provider()
    if provider is None:
        return model
    from pydantic_ai.models.instrumented import InstrumentationSettings

    return InstrumentedModel(
        model,
        InstrumentationSettings(tracer_provider=provider, include_content=True),
    )
