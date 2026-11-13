import asyncio
import logging
import signal
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from psycopg_pool import ConnectionPool
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import diagnostic_echo
from app.chat_dispatcher import ChatActivities, dispatch_chats
from app.chat_model import chat_agent
from app.chat_repository import ChatRepository
from app.chat_workflow import ChatWorkflow
from app.config import get_settings
from app.generation_activities import GenerationActivities
from app.generation_dispatcher import dispatch_generations
from app.generation_repository import GenerationRepository
from app.generation_workflow import GenerationWorkflow
from app.import_activities import ImportActivities
from app.import_dispatcher import dispatch_imports
from app.import_repository import ImportRepository
from app.review_activities import ReviewActivities
from app.review_dispatcher import dispatch_reviews
from app.review_repository import ReviewRepository
from app.review_workflow import ReviewWorkflow
from app.workflows import DiagnosticWorkflow, ImportWorkflow, LegacyImportWorkflow
from worker.agent.read_adapter import configure_read_port


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        plugins=[PydanticAIPlugin()],
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    pool = ConnectionPool(
        kwargs={
            "host": settings.postgres_host,
            "dbname": settings.postgres_db,
            "user": settings.postgres_user,
            "password": settings.postgres_password.get_secret_value(),
            "connect_timeout": 2,
        },
        min_size=0,
        max_size=5,
        timeout=5,
    )
    imports = ImportActivities(pool, settings)
    reviews = ReviewActivities(pool, settings)
    generations = GenerationActivities(pool)
    chat_repository = ChatRepository(pool, settings.temporal_namespace)
    chat_activities = ChatActivities(chat_repository)
    if chat_agent:
        configure_read_port(pool)
    if chat_agent:
        ChatWorkflow.__pydantic_ai_agents__ = [chat_agent]
    executor = ThreadPoolExecutor(max_workers=4)
    async with Worker(
        client,
        task_queue=settings.temporal_task_queue,
        identity=f"education-worker@{socket.gethostname()}",
        workflows=[
            DiagnosticWorkflow,
            LegacyImportWorkflow,
            ImportWorkflow,
            ReviewWorkflow,
            GenerationWorkflow,
            *([ChatWorkflow] if chat_agent else []),
        ],
        activities=[
            diagnostic_echo,
            imports.begin,
            imports.parse,
            imports.failed,
            imports.finish,
            imports.prepare_v2,
            imports.parse_v2,
            imports.finish_v2,
            reviews.execute,
            reviews.failed,
            generations.build,
            generations.failed,
            chat_activities.mark_running,
            chat_activities.mark_failed,
            chat_activities.save_result,
        ],
        activity_executor=executor,
        max_concurrent_activities=2,
        graceful_shutdown_timeout=timedelta(seconds=5),
    ):
        logging.info(
            "Temporal Worker started: namespace=%s queue=%s",
            settings.temporal_namespace,
            settings.temporal_task_queue,
        )
        dispatcher = asyncio.create_task(
            dispatch_imports(
                client,
                ImportRepository(pool),
                settings.temporal_task_queue,
                stop,
            )
        )
        review_dispatcher = asyncio.create_task(
            dispatch_reviews(client, ReviewRepository(pool), settings.temporal_task_queue, stop)
        )
        generation_dispatcher = asyncio.create_task(
            dispatch_generations(
                client, GenerationRepository(pool), settings.temporal_task_queue, stop
            )
        )
        chat_dispatcher = (
            asyncio.create_task(
                dispatch_chats(client, chat_repository, settings.temporal_task_queue, stop)
            )
            if chat_agent
            else asyncio.create_task(stop.wait())
        )
        try:
            await stop.wait()
        finally:
            stop.set()
            await dispatcher
            await review_dispatcher
            await generation_dispatcher
            await chat_dispatcher
    executor.shutdown(wait=True)
    pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
