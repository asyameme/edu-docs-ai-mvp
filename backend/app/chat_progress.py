"""Понятные пользователю события реальной работы агента."""

from dataclasses import dataclass, field

from pydantic_ai.capabilities import AbstractCapability
from temporalio import workflow


def tool_progress_label(name, args):
    if name == "search_entities":
        kinds = set(args.get("entity_kinds") or [])
        if kinds == {"competency"}:
            return "Ищу компетенции…"
        if kinds == {"indicator"}:
            return "Ищу индикаторы…"
        if kinds and kinds <= {"discipline", "curriculum_item"}:
            return "Ищу дисциплины в программе…"
        return "Ищу данные программы…"
    return {
        "read_entity": "Читаю данные выбранного элемента…",
        "read_source_fragment": "Читаю фрагмент документа…",
        "read_graph_projection": "Изучаю связи в программе…",
        "get_validation_findings": "Изучаю замечания к программе…",
        "list_program_check_targets": "Ищу данные для проверки…",
        "request_program_check": "Запускаю проверку программы…",
        "read_program_check": "Читаю результаты проверки…",
    }.get(name, "Обрабатываю данные программы…")


@dataclass
class ChatProgressState:
    message: str = "Обдумываю запрос…"
    active: dict[str, str] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)

    def start_tool(self, call_id, label):
        self.active[call_id] = label
        self.steps = [*self.steps[-7:], label]

    def finish_tool(self, call_id):
        self.active.pop(call_id, None)
        self.message = "Анализирую результаты…"

    def model_request(self):
        self.message = ("Анализирую результаты и готовлю ответ…"
                        if self.steps else "Обдумываю запрос…")

    def snapshot(self):
        labels = list(dict.fromkeys(self.active.values()))
        return {"message": " · ".join(labels) if labels else self.message,
                "steps": list(self.steps)}


def _state():
    if workflow.in_workflow():
        return getattr(workflow.instance(), "chat_progress", None)
    return None


class ChatProgressCapability(AbstractCapability):
    async def before_model_request(self, ctx, request_context):
        if (state := _state()) is not None:
            state.model_request()
        return request_context

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        state = _state()
        if state is not None:
            state.start_tool(call.tool_call_id, tool_progress_label(tool_def.name, args))
        try:
            return await handler(args)
        finally:
            if state is not None:
                state.finish_tool(call.tool_call_id)
