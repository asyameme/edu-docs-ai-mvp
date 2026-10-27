"""Повторная доставка сохранённых задач: окно между commit и Temporal закрывается retry."""

import asyncio
import logging

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.workflows import ImportWorkflow


async def dispatch_imports(client, repo, queue, stop):
    while not stop.is_set():
        try:
            pending = await asyncio.to_thread(repo.pending)
            for job_id, attempt in pending:
                try:
                    await client.start_workflow(
                        ImportWorkflow.run,
                        {"job_id": str(job_id), "attempt": attempt},
                        id=f"import-{job_id}-{attempt}",
                        task_queue=queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
                await asyncio.to_thread(repo.dispatched, job_id, attempt)
        except Exception:
            logging.exception("Не удалось доставить задачи импорта; доставка будет повторена")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass
