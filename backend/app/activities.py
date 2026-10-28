import socket
from uuid import uuid4

from temporalio import activity
from temporalio.exceptions import ApplicationError

# Модуль работает вне изолированной среды Workflow. Новый процесс — новая сессия.
WORKER_SESSION_ID = uuid4().hex


@activity.defn
async def diagnostic_echo(marker: str) -> dict[str, str]:
    if not marker or len(marker) > 128:
        raise ApplicationError("Invalid diagnostic marker", non_retryable=True)
    return {"marker": marker, "worker": socket.gethostname(), "session": WORKER_SESSION_ID}
