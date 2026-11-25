"""Повторная доставка сохранённых задач генерации в Temporal."""

import asyncio
import logging

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.generation_workflow import GenerationWorkflow
from app.temporal_rpc import DELIVERY_TIMEOUT_SECONDS, temporal_rpc


async def dispatch_generations(client, repo, queue, stop):
    while not stop.is_set():
        try:
            for job_id, workflow_id in await asyncio.to_thread(repo.pending):
                if stop.is_set():
                    return
                try:
                    await temporal_rpc(
                        client.start_workflow(
                            GenerationWorkflow.run,
                            {"job_id": str(job_id)},
                            id=workflow_id,
                            task_queue=queue,
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        ),
                        timeout=DELIVERY_TIMEOUT_SECONDS,
                    )
                except WorkflowAlreadyStartedError:
                    pass
        except Exception:
            logging.exception("Доставка задачи генерации будет повторена")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass
