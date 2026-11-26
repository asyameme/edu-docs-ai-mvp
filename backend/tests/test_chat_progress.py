"""Статусы отражают реальные действия, а не текст запроса пользователя."""

from app.chat_progress import ChatProgressState, tool_progress_label


def test_search_status_uses_validated_entity_kinds():
    label = tool_progress_label
    assert label("search_entities", {"entity_kinds": ["competency"]}) == "Ищу компетенции…"
    assert label("search_entities", {"entity_kinds": ["indicator"]}) == "Ищу индикаторы…"
    assert label("search_entities", {"query": "секретный текст"}) == "Ищу данные программы…"


def test_parallel_tools_remain_visible_until_each_finishes():
    state = ChatProgressState()
    state.start_tool("a", "Ищу компетенции…")
    state.start_tool("b", "Читаю документ…")
    state.finish_tool("a")
    assert state.snapshot()["message"] == "Читаю документ…"
    state.finish_tool("b")
    assert state.snapshot()["message"] == "Анализирую результаты…"
    assert state.snapshot()["steps"] == ["Ищу компетенции…", "Читаю документ…"]


def test_progress_history_is_bounded_and_does_not_expose_tool_identifiers():
    state = ChatProgressState()
    for i in range(20):
        state.start_tool(str(i), "Читаю документ…")
        state.finish_tool(str(i))
    snapshot = state.snapshot()
    assert len(snapshot["steps"]) <= 8
    assert "active" not in snapshot


def test_tool_error_does_not_leave_an_active_status(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pytest

    import app.chat_progress as progress

    state = ChatProgressState()
    monkeypatch.setattr(progress, "_state", lambda: state)

    async def handler(args):
        assert state.snapshot()["message"] == "Ищу компетенции…"
        raise ValueError("Внутренняя ошибка")

    with pytest.raises(ValueError):
        asyncio.run(progress.ChatProgressCapability().wrap_tool_execute(None,
            call=SimpleNamespace(tool_call_id="call"),
            tool_def=SimpleNamespace(name="search_entities"),
            args={"entity_kinds": ["competency"]}, handler=handler))
    assert not state.active
    assert "Внутренняя ошибка" not in str(state.snapshot())
