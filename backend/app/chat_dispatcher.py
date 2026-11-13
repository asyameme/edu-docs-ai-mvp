"""Повторная доставка записанных ходов чата в Temporal."""

import asyncio
import logging
from datetime import timedelta

from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.chat_workflow import ChatWorkflow


async def dispatch_chats(client, repo, queue, stop):
    while not stop.is_set():
        try:
            after = None
            while True:
                batch = await asyncio.to_thread(repo.pending, after)
                if not batch:
                    break
                for turn_id, workflow_id, created_at in batch:
                    after = (created_at, turn_id)
                    payload = await asyncio.to_thread(repo.turn_input, turn_id)
                    try:
                        await client.start_workflow(
                            ChatWorkflow.run,
                            {
                                "turn": str(turn_id),
                                "text": payload["user_text"],
                                "program_id": str(payload["program_id"]),
                                "version_id": str(payload["version_id"]),
                                "conversation": payload["conversation"].decode()
                                if payload["conversation"]
                                else None,
                            },
                            id=workflow_id,
                            task_queue=queue,
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            execution_timeout=timedelta(minutes=15),
                        )
                    except WorkflowAlreadyStartedError:
                        description = await client.get_workflow_handle(workflow_id).describe()
                        if description.status.name != "RUNNING":
                            await asyncio.to_thread(repo.fail, turn_id)
        except Exception:
            logging.exception("Доставка сообщения чата будет повторена")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass


class ChatActivities:
    def __init__(self, repo):
        self.repo = repo

    @activity.defn(name="chat_mark_running_v1")
    async def mark_running(self, turn_id: str) -> None:
        return None

    @activity.defn(name="chat_mark_failed_v1")
    async def mark_failed(self, turn_id: str) -> None:
        await asyncio.to_thread(self.repo.fail, turn_id)

    @activity.defn(name="chat_save_result_v1")
    async def save_result(self, payload: dict) -> None:
        await asyncio.to_thread(
            self.repo.complete,
            payload["turn_id"],
            payload["result"],
            payload["conversation"].encode(),
        )
