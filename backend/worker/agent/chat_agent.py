"""Pydantic AI behavior and read-only tools for the program chat."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModelSettings
from temporalio.common import RetryPolicy

from app.chat_findings import FindingsPage
from app.chat_progress import ChatProgressCapability

from .contracts import (
    AgentOutput,
    ChatDeps,
    EntityReadResult,
    EntitySearchResult,
    GraphProjection,
    SourceFragment,
    ValidationFinding,
)
from .llm_journal import journalled
from .read_port import EntityKind, get_worker_read_port


def _chat_model_settings(model_name: str) -> OpenAIChatModelSettings:
    """Размышления модели — главная причина долгих ответов в чате.

    Qwen на vLLM размышляет по умолчанию и тратит лимит токенов на reasoning
    (костыль как в validation/semantic_llm.py). Kimi K3 размышляет подолгу на
    каждом шаге агентного цикла — просим низкие усилия, как в валидации.
    """

    name = (model_name or "").lower()
    if "kimi" in name or "k3" in name:
        return OpenAIChatModelSettings(extra_body={"reasoning_effort": "low"})
    return OpenAIChatModelSettings(
        extra_body={"chat_template_kwargs": {"enable_thinking": False}}
    )

INSTRUCTIONS = """
Ты чат-агент системы поддержки документации образовательной программы.

- Отвечай кратко и по-русски; перечисляй только факты, проверенные инструментами.
- Для утверждений о состоянии программы используй результаты инструментов.
- Не придумывай значения, связи, формулировки или ссылки на источники.
- Если сущность или данные не нашлись — скажи об этом прямо, без догадок.
- Если данные сокращены (truncated=true), используй summary/matched_context;
  для нужного фрагмента передай в read_entity query с искомой формулировкой.
- Если запрос неоднозначен, верни Clarification с конкретными вопросами.
- Короткий ответ на предыдущий Clarification продолжает обсуждение той же
  дисциплины и правки. Возьми из истории документ, поле и новое значение.
  Учти уже полученные ответы и уточняй только то, что осталось неизвестным.
- Если ограничение системы нельзя снять ответом пользователя, сообщи о нём
  как о факте. Не спрашивай, нужно ли его учесть.
- Если в истории есть ChangeProposal с unresolved_questions, считай следующий запрос
  продолжением этого предложения: сверяй его с каждым открытым вопросом.
  Если пользователь ответил однозначно, учти ответ, перечитай затронутую сущность,
  пересобери предложение и убери только разрешённые вопросы. Не повторяй прежнее
  предложение без изменений.
  Если ответ не разрешает вопрос или новое сообщение слишком общее, верни
  Clarification с точным вопросом. Не создавай новое предложение, пока неясно,
  какой именно параметр или вариант пользователь выбрал.
- Если пользователь просит изменить данные, сначала покажи текущее состояние
  сущности (read_entity или read_graph_projection) и только затем верни
  ChangeProposal с текущими и предлагаемыми значениями и источниками.
- Для правки конкретного документа ограничь search_entities нужным типом.
  Строки учебного плана — curriculum_item, дисциплины РПД/РПП — discipline.
  Ищи по названию в именительном падеже, например «Глубокое обучение».
  Прочитай только строки, которые могут быть целью правки. Открывай текст
  источника, только если в графе не хватает конкретного значения.
- Особый случай: если пользователь просит изменить общий объём в РПД и
  указывает только итоговое число часов, верни RpdWorkloadAlignment с точным
  названием дисциплины и целевым итогом. Укажи только явно названные семестры.
  Сервер сверит итог с исходными строками семестров и составит план или уточнит
  графы нагрузки. Используй RpdWorkloadAlignment даже если уже прочитал сущность.
- Для правки часов прочитай строку учебного плана или дисциплину РПД через
  read_entity. В РПД разные профили могут быть записаны как отдельные сущности.
  Если инструмент вернул editable_workload, итог считается автоматически: составь изменения
  отдельных граф с указанными в инструменте field_path, часами и source_ref_id.
  При просьбе перенести разбивку из РПД сначала прочитай её и сопоставь те же
  семестры и виды нагрузки. Предлагай только отличающиеся значения.
  Для учебного плана, если пользователь назвал только итог, уточни, какие
  графы менять, и покажи текущие часы. Если графы
  недоступны и есть hours_total_write_error, объясни ограничение без плана.
- Предлагая правку того же поля повторно, сначала перечитай сущность:
  значение могло измениться с прошлой правки.
- На общий вопрос о проблемах, нарушениях и замечаниях сначала используй
  get_validation_summary и ответь по готовой сводке без read_entity,
  search_entities и read_source_fragment для каждого замечания. Не перепроверяй
  все документы ради общего списка. Не запускай новую проверку без просьбы.
  По умолчанию показывай оставшиеся проблемы (status=attention).
  Если просят также принятые или решённые замечания, передай status=all.
  При has_more=true назови общее число групп и сообщи, что показана часть;
  следующую страницу получай по просьбе пользователя с after=next_cursor.
  Группы могут иметь смешанные status_counts: не называй принятые записи нерешёнными.
  Названия документов уже есть в documents; неизвестные сведения не додумывай.
  Для подробного разбора выбранного замечания читай нужные source_ref_ids
  и сущности; get_validation_findings остаётся инструментом отдельных записей.
  Для подготовки конкретной правки не загружай общий список замечаний.
  Результаты повторной проверки появляются после применения плана.
- По явной просьбе проверить всю программу вызывай request_program_check без
  параметров выбора. Если пользователь просит проверить конкретную компетенцию
  по R-09 или конкретную РПД по R-11, сначала вызови
  list_program_check_targets с коротким кодом/названием из запроса. Если найден
  ровно один вариант, передай его точный competency_code или document_id в
  request_program_check. Если вариантов нет или их несколько, задай уточняющий
  вопрос и не запускай проверку всей программы вместо точечной. Если результат
  truncated=true, попроси более точный код или фрагмент названия. После запуска
  называй выбранную цель из результата; если проверка ещё выполняется, сохрани
  check_job_id и используй read_program_check для получения результата.
- В get_validation_findings статус accepted означает, что пользователь уже
  принял предупреждение; resolved — что он уже выбрал вариант. В полном списке
  показывай такие пункты с пометкой, что они уже приняты или решены, и не проси
  подтверждать их повторно. На вопрос о том, что ещё требует внимания,
  отвечай по open и needs_review.
- Описывая расхождения или замечания, пиши человекочитаемо, для сотрудника
  деканата, а не для разработчика:
  — структурируй ответ короткими абзацами и списком, по одному расхождению
    на строку, с новой строки (переносы строк сохраняются в интерфейсе);
  — для каждого расхождения называй дисциплину, семестр и оба документа:
    что написано в учебном плане (раздел/семестр) и что в РПД (имя файла);
  — переводи служебные названия полей и единиц на обычный язык:
    «hours_total» → «общий объём часов», «credit_units» → «зачётные единицы»;
    числа давай с единицами: «144 часа», «4 зачётные единицы»;
  — не упоминай внутренние коды статусов и полей («warning», «open»,
    «status», имена атрибутов); код правила можно дать один раз в конце
    строки в скобках для навигации, например «(правило R-02)»;
  — группируй связанные замечания одной дисциплины вместе и объясняй
    суть расхождения словами («в РПД указано больше часов, чем в плане»).
- ChangeProposal является только предложением. Не утверждай, что данные изменены.
- Применяет предложение пользователь кнопкой «Применить план» в интерфейсе.
  Не задавай вопрос «подтверждаете ли применение» и не клади его в
  unresolved_questions: пока список не пуст, кнопка заблокирована. Клади туда
  только вопросы о недостающих данных (какая сущность, какое значение, какой
  вариант); если правка определена полностью — оставь список пустым.
- Если пользователь пишет «да», «применяй», «подтверждаю», а предложение уже
  показано, не собирай его заново: ответь, что применить его можно кнопкой
  «Применить план». Новое предложение нужно только при изменившемся запросе.
- Итог проверки приводи по полю summary из read_program_check, с числами:
  сколько правил пройдено, сколько с замечаниями, сколько пропущено.
  all_rules_passed=false значит «не все пройдены», а не «не пройдено ни одно».
- Приводя часы из строки нагрузки, называй вид нагрузки — он записан в атрибутах
  сущности (например, «лекции — 28 часов, практические занятия — 24 часа»).
  Число без названия графы не приводи и не оставляй без объяснения.
- Если у сущности заполнены часы по видам нагрузки с источниками
  (hours_by_kind), общий объём считай их суммой и правь виды нагрузки: каждой
  правке вида — свой источник-ячейку, а пересчитанный итоговый объём предложи
  отдельной правкой hours_total в том же плане. Сам итоговый объём напрямую
  не правь: он вычислен из видов, и его правка будет отклонена.
- Не выбирай канонический код или формулировку при расхождении источников.
  Покажи варианты и попроси пользователя выбрать.
""".strip()


def create_chat_agent(
    model: Model,
    *,
    temporal_models: Mapping[str, Model] | None = None,
) -> Agent[ChatDeps, AgentOutput]:
    """Создаёт агента с необязательными алиасами моделей для backend Temporal."""

    async def bound_context(ctx: RunContext[ChatDeps], messages):
        if ctx.deps.context_tokens is None:
            return messages
        from app.chat_context import CONTEXT_RESERVE_TOKENS, trim_messages

        return trim_messages(messages, ctx.deps.context_tokens - CONTEXT_RESERVE_TOKENS)

    agent = Agent(
        journalled(model),
        name="edu_program_chat_v1",
        deps_type=ChatDeps,
        output_type=AgentOutput,
        instructions=INSTRUCTIONS,
        capabilities=[
            ChatProgressCapability(),
            ProcessHistory(processor=bound_context),
            TemporalDurability(
                models=temporal_models,
                # Не даём сетевой ошибке модели бесконечно удерживать ход до лимита workflow.
                # Reasoning-модели на большом контексте отвечают дольше минуты:
                # без явного таймаута activity обрезается дефолтными 60 секундами.
                model_activity_config={
                    "retry_policy": RetryPolicy(maximum_attempts=3),
                    "start_to_close_timeout": timedelta(seconds=300),
                },
            )
        ],
        model_settings=_chat_model_settings(getattr(model, "model_name", "")),
        retries=2,
    )

    @agent.tool
    async def search_entities(
        ctx: RunContext[ChatDeps],
        query: Annotated[str, Field(min_length=1, max_length=500)],
        entity_kinds: list[EntityKind] | None = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> EntitySearchResult:
        """Находит сущности по имени или содержимому в выбранной версии программы."""

        return await get_worker_read_port().search_entities(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            query=query,
            entity_kinds=entity_kinds,
            limit=limit,
        )

    @agent.tool
    async def read_entity(
        ctx: RunContext[ChatDeps],
        entity_id: UUID,
        query: Annotated[str | None, Field(min_length=1, max_length=500)] = None,
    ) -> EntityReadResult:
        """Читает сущность, её прямые связи и ссылки на источники."""

        return await get_worker_read_port().read_entity(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            entity_id=entity_id,
            query=query,
        )

    @agent.tool
    async def read_source_fragment(
        ctx: RunContext[ChatDeps],
        source_ref_id: UUID,
        context_paragraphs: Annotated[int, Field(ge=0, le=2)] = 1,
    ) -> SourceFragment:
        """Читает фрагмент источника и небольшой окружающий контекст."""

        return await get_worker_read_port().read_source_fragment(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            source_ref_id=source_ref_id,
            context_paragraphs=context_paragraphs,
        )

    @agent.tool
    async def read_graph_projection(
        ctx: RunContext[ChatDeps],
        entity_id: UUID,
        depth: Annotated[int, Field(ge=0, le=2)] = 1,
    ) -> GraphProjection:
        """Читает ограниченную часть графа вокруг сущности."""

        return await get_worker_read_port().read_graph_projection(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            entity_id=entity_id,
            depth=depth,
        )

    @agent.tool
    async def get_validation_summary(
        ctx: RunContext[ChatDeps],
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
        after: str | None = None,
        status: Literal["all", "attention"] = "attention",
    ) -> FindingsPage:
        """Готовый обзор автоматической сверки и основной проверки версии.

        Отдельно запущенные дополнительные проверки R-09/R-11 сюда не входят.

        Для общего вопроса достаточно первой страницы. При has_more укажи,
        что список неполный; следующую страницу читай по просьбе пользователя.
        status=attention выбирает только то, что ещё требует внимания.
        """
        return await get_worker_read_port().get_validation_summary(
            program_id=ctx.deps.program_id, version_id=ctx.deps.version_id,
            limit=limit, after=after, status=status,
        )

    @agent.tool
    async def get_validation_findings(
        ctx: RunContext[ChatDeps],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[ValidationFinding]:
        """Читает замечания сверки и проверки, связанные с выбранной версией.

        Статусы accepted и resolved означают, что предупреждение уже принято
        или пользователь уже выбрал вариант.
        """

        return await get_worker_read_port().get_validation_findings(
            program_id=ctx.deps.program_id, version_id=ctx.deps.version_id, limit=limit
        )

    return agent
