import asyncio
import logging
import signal
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from psycopg_pool import ConnectionPool
from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import diagnostic_echo
from app.config import get_settings
from app.import_activities import ImportActivities
from app.import_dispatcher import dispatch_imports
from app.import_repository import ImportRepository
from app.workflows import DiagnosticWorkflow, ImportWorkflow, LegacyImportWorkflow


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
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
    executor = ThreadPoolExecutor(max_workers=4)
    async with Worker(
        client,
        task_queue=settings.temporal_task_queue,
        identity=f"education-worker@{socket.gethostname()}",
        workflows=[DiagnosticWorkflow, LegacyImportWorkflow, ImportWorkflow],
        activities=[
            diagnostic_echo,
            imports.begin,
            imports.parse,
            imports.failed,
            imports.finish,
            imports.prepare_v2,
            imports.parse_v2,
            imports.finish_v2,
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
        try:
            await stop.wait()
        finally:
            stop.set()
            await dispatcher
    executor.shutdown(wait=True)
    pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
