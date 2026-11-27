"""Проверка реальных чтений и устойчивости Temporal в отдельной очереди.

База открывается только на чтение; тестовые chat activities не пишут историю продукта.
"""

import asyncio
import os
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("CHAT_FINDINGS_RUNTIME") != "1", reason="Требуются локальные БД и Temporal"
)


def runtime_pool():
    from psycopg_pool import ConnectionPool

    from app.config import get_settings

    s = get_settings()
    return ConnectionPool(
        kwargs=dict(
            host=s.postgres_host,
            dbname=s.postgres_db,
            user=s.postgres_user,
            password=s.postgres_password.get_secret_value(),
            options="-c default_transaction_read_only=on",
        ),
        min_size=0,
        max_size=2,
    )


def target(pool):
    program_id = UUID(os.environ["CHAT_FINDINGS_PROGRAM"])
    with pool.connection() as conn:
        version = conn.execute(
            "SELECT current_version_id FROM programs WHERE id=%s", (program_id,)
        ).fetchone()[0]
    return program_id, version


def test_real_summary_matches_current_review_api():
    import httpx

    from app.chat_findings import build_findings_page
    from app.chat_findings_store import read_findings_page

    with runtime_pool() as pool:
        program, version = target(pool)
        actual = read_findings_page(pool, program, version, status="attention", limit=20)
        response = httpx.get(
            f"http://api:8000/api/v1/programs/{program}/versions/{version}/review",
            params={"limit": 100, "offset": 0},
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        assert data["issues_count"] == len(data["issues"])
        expected = build_findings_page(
            data["issues"],
            version_id=version,
            documents={},
            references={},
            status="attention",
            limit=20,
        )
        assert actual.total_groups == expected.total_groups
        assert actual.total_records == expected.total_records
        assert {g.id for g in actual.groups} == {g.id for g in expected.groups}
        assert actual.groups and all(g.documents for g in actual.groups)
        print(
            "Сводка совпадает с API:",
            actual.total_groups,
            "групп,",
            actual.total_records,
            "записей",
        )


def test_temporal_limit_keeps_summary_and_replays(monkeypatch):
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

    import app.chat_workflow as module
    from app.chat_model import _create_configured_chat_agent
    from app.config import get_settings
    from worker.agent import read_port
    from worker.agent.read_adapter import PostgresChatReadPort

    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart("get_validation_summary", {"status": "attention"})
                for _ in range(1 if calls == 1 else 3)
            ]
        )

    agent = _create_configured_chat_agent(FunctionModel(model))
    monkeypatch.setattr(module, "chat_agent", agent)
    monkeypatch.setattr(module.ChatWorkflow, "__pydantic_ai_agents__", [agent])
    saved = []

    @activity.defn(name="chat_mark_running_v1")
    async def mark_running(arg):
        pass

    @activity.defn(name="chat_mark_failed_v1")
    async def mark_failed(arg):
        pass

    @activity.defn(name="chat_save_result_v1")
    async def save_result(arg):
        saved.append(arg)

    async def run():
        s = get_settings()
        client = await Client.connect(
            s.temporal_address, namespace=s.temporal_namespace, plugins=[PydanticAIPlugin()]
        )
        with runtime_pool() as pool:
            monkeypatch.setattr(read_port, "_worker_read_port", PostgresChatReadPort(pool))
            program, version = target(pool)
            queue = "findings-test-" + uuid4().hex
            async with Worker(
                client,
                task_queue=queue,
                workflows=[module.ChatWorkflow],
                activities=[mark_running, mark_failed, save_result],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = await client.start_workflow(
                    module.ChatWorkflow.run,
                    {
                        "turn": str(uuid4()),
                        "program_id": str(program),
                        "version_id": str(version),
                        "text": "Покажи проблемы",
                    },
                    id=queue,
                    task_queue=queue,
                )
                result = await asyncio.wait_for(handle.result(), 120)
                assert "Уже полученные замечания" in result["message"]
                assert "не завершён" in result["message"]
                assert saved[0]["result"] == result
                history = await handle.fetch_history()
            await Replayer(
                workflows=[module.ChatWorkflow],
                plugins=[PydanticAIPlugin()],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ).replay_workflow(history)
            # Старый сбой также должен воспроизводиться без новой ветки fallback.
            old = await client.get_workflow_handle(
                "chat-551586c4-2518-42fb-b76a-7e4148bbcd4e"
            ).fetch_history()
            await Replayer(
                workflows=[module.ChatWorkflow],
                plugins=[PydanticAIPlugin()],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ).replay_workflow(old)
            print(
                "Temporal: частичная сводка сохранена; новое и прежнее выполнения replay пройдены"
            )

    asyncio.run(run())


@pytest.mark.skipif(os.getenv("CHAT_FINDINGS_LIVE") != "1", reason="Нужна настроенная модель")
@pytest.mark.parametrize("prompt", ["Покажи проблемы", "Какие замечания ещё требуют внимания?"])
def test_live_overview_uses_summary_without_document_walk(monkeypatch, prompt):
    from pydantic_ai import UsageLimits
    from test_agent_live import _live_model

    from worker.agent import read_port
    from worker.agent.chat_agent import create_chat_agent
    from worker.agent.contracts import Answer, ChatDeps
    from worker.agent.read_adapter import PostgresChatReadPort

    with runtime_pool() as pool:
        program, version = target(pool)
        port = PostgresChatReadPort(pool)
        calls = []
        for name in (
            "get_validation_summary",
            "get_validation_findings",
            "read_entity",
            "search_entities",
            "read_source_fragment",
            "read_graph_projection",
        ):
            original = getattr(port, name)

            def tracked(fn, label):
                async def call(**kwargs):
                    calls.append(label)
                    return await fn(**kwargs)

                return call

            monkeypatch.setattr(port, name, tracked(original, name))
        monkeypatch.setattr(read_port, "_worker_read_port", port)
        agent = create_chat_agent(_live_model())
        result = asyncio.run(
            agent.run(
                prompt,
                deps=ChatDeps(program_id=program, version_id=version, context_tokens=200_000),
                usage_limits=UsageLimits(
                    request_limit=8, tool_calls_limit=12, output_tokens_limit=4000
                ),
            )
        )
        assert isinstance(result.output, Answer)
        assert calls == ["get_validation_summary"], calls
        assert len(result.output.message) > 100
        print("Живая модель:", prompt, "→", calls, "длина ответа", len(result.output.message))


def test_summary_store_filters_versions_jobs_and_resolved_rows_before_page():
    """Временные таблицы изолируют проверку SQL от документов рабочей программы."""
    from contextlib import contextmanager

    import psycopg
    from psycopg.types.json import Jsonb

    from app.chat_findings_store import read_findings_page
    from app.config import get_settings

    s = get_settings()
    with psycopg.connect(
        host=s.postgres_host,
        dbname=s.postgres_db,
        user=s.postgres_user,
        password=s.postgres_password.get_secret_value(),
    ) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("CREATE TEMP TABLE program_versions(id uuid,program_id uuid,review jsonb)")
            conn.execute("""CREATE TEMP TABLE processing_jobs(id uuid,program_id uuid,
                input_version_id uuid,type text,parameters jsonb,report jsonb,
                created_at timestamptz)""")
            conn.execute("""CREATE TEMP TABLE validation_issues(id uuid,job_id uuid,program_id uuid,
                version_id uuid,rule_code text,type text,severity text,status text,details jsonb,
                created_at timestamptz)""")
            conn.execute("CREATE TEMP TABLE source_refs(id uuid,program_id uuid,document_id uuid)")
            conn.execute(
                "CREATE TEMP TABLE source_documents(id uuid,program_id uuid,original_name text)"
            )
            program, version, review, check, manual, doc = [uuid4() for _ in range(6)]
            resolved = [uuid4() for _ in range(60)]
            conn.execute(
                "INSERT INTO program_versions VALUES(%s,%s,%s)",
                (
                    version,
                    program,
                    Jsonb(
                        {
                            "review_id": str(review),
                            "decisions": [{"conflict_id": str(i)} for i in resolved],
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO source_documents VALUES(%s,%s,%s)", (doc, program, "РПД.docx")
            )
            conn.execute(
                "INSERT INTO processing_jobs VALUES(%s,%s,%s,'validate',%s,%s,now())",
                (
                    check,
                    program,
                    version,
                    Jsonb({"purpose": "check"}),
                    Jsonb({"outcome": "completed"}),
                ),
            )
            rows = [
                (
                    i,
                    review,
                    program,
                    version,
                    "R-02",
                    "hours",
                    "warning",
                    "open",
                    Jsonb({"message": "Уже решено"}),
                )
                for i in resolved
            ]
            for job, owner, rule, message in [
                (check, program, "R-04", "Актуальная проблема"),
                (review, program, "R-08", "Устаревшее замечание"),
                (manual, program, "R-11", "Отдельная ручная проверка"),
                (check, uuid4(), "R-02", "Чужая программа"),
            ]:
                rows.append(
                    (
                        uuid4(),
                        job,
                        owner,
                        version,
                        rule,
                        "hours",
                        "warning",
                        "open",
                        Jsonb({"message": message, "source_document_ids": [str(doc)]}),
                    )
                )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO validation_issues VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,now())", rows
                )

            class Pool:
                @contextmanager
                def connection(self):
                    yield conn

            result = read_findings_page(Pool(), program, version, status="attention", limit=1)
            assert result.total_groups == result.total_records == 1
            assert result.groups[0].title == "Актуальная проблема"
            assert result.groups[0].documents[0].name == "РПД.docx"
            assert not result.has_more
