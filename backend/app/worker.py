import asyncio
import logging
import signal
import socket
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import diagnostic_echo
from app.config import get_settings
from app.workflows import DiagnosticWorkflow


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with Worker(
        client,
        task_queue=settings.temporal_task_queue,
        identity=f"education-worker@{socket.gethostname()}",
        workflows=[DiagnosticWorkflow],
        activities=[diagnostic_echo],
        max_concurrent_activities=2,
        graceful_shutdown_timeout=timedelta(seconds=5),
    ):
        logging.info(
            "Temporal Worker started: namespace=%s queue=%s",
            settings.temporal_namespace,
            settings.temporal_task_queue,
        )
        await stop.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
