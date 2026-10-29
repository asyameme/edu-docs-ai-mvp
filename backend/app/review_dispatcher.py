"""Повторная доставка бизнес-запросов на анализ без SQL-состояния выполнения."""

import asyncio
import logging

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.review_workflow import ReviewWorkflow


async def dispatch_reviews(client, repo, queue, stop):
    while not stop.is_set():
        try:
            for job_id, workflow_id in await asyncio.to_thread(repo.pending):
                try:
                    await client.start_workflow(
                        ReviewWorkflow.run,
                        {"job_id": str(job_id)},
                        id=workflow_id,
                        task_queue=queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
        except Exception:
            logging.exception("Доставка анализа будет повторена")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass
