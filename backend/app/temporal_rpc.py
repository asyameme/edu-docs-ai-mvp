"""Ограничение полного ожидания RPC, включая внутренние повторы Temporal SDK."""

import asyncio
from collections.abc import Awaitable

STATUS_TIMEOUT_SECONDS = 3
DELIVERY_TIMEOUT_SECONDS = 5


async def temporal_rpc[T](request: Awaitable[T], *, timeout: float | None = None) -> T:
    return await asyncio.wait_for(
        request, timeout=STATUS_TIMEOUT_SECONDS if timeout is None else timeout
    )
