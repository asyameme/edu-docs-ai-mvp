import asyncio
import socket
from datetime import timedelta

from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace, lazy=True
    )
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=settings.temporal_namespace,
            task_queue=TaskQueue(name=settings.temporal_task_queue),
            task_queue_type=1,
        ),
        timeout=timedelta(seconds=3),
    )
    identity = f"education-worker@{socket.gethostname()}"
    if not any(poller.identity == identity for poller in response.pollers):
        raise RuntimeError("This worker is not polling the Workflow task queue")


if __name__ == "__main__":
    asyncio.run(main())
