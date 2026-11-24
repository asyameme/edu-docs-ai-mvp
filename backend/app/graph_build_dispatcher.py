"""Redeliver graph builds persisted by the decision transaction."""

import asyncio
import logging

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.graph_build_workflow import GraphBuildWorkflow


async def dispatch_graph_builds(client, repo, queue, stop):
    while not stop.is_set():
        try:
            for job_id, workflow_id in await asyncio.to_thread(repo.graph_build_pending):
                try:
                    await client.start_workflow(
                        GraphBuildWorkflow.run,
                        {"job_id": str(job_id)},
                        id=workflow_id,
                        task_queue=queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
        except Exception:
            logging.exception("Доставка сборки графа будет повторена")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass
