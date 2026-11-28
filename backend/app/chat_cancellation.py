import asyncio
import logging
from datetime import timedelta

from temporalio.service import RPCError, RPCStatusCode

from app.temporal_rpc import DELIVERY_TIMEOUT_SECONDS, temporal_rpc

logger = logging.getLogger(__name__)


async def cancel_chat(client, repository, turn):
    try:
        await temporal_rpc(
            client.get_workflow_handle(turn["workflow_id"]).terminate(
                reason="Остановлено пользователем",
                rpc_timeout=timedelta(seconds=DELIVERY_TIMEOUT_SECONDS),
            ),
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            logger.warning("Остановка чата будет повторена: %s", error.status.name)
            return
    except TimeoutError:
        logger.warning("Истекло время ожидания остановки чата")
        return
    await asyncio.to_thread(repository.finish_cancel, turn["id"])
