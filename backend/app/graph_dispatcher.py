"""Повторная доставка задач и сверка восстановимого графа с SQL-версиями."""

import asyncio
import logging
import time
from datetime import timedelta

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.graph_workflow import GraphWorkflow


async def dispatch_graphs(client, repo, store, queue, stop):
    next_reconcile = 0
    while not stop.is_set():
        try:
            for job_id, workflow_id in await asyncio.to_thread(repo.pending):
                try:
                    await client.start_workflow(
                        GraphWorkflow.run,
                        {"job_id": str(job_id)},
                        id=workflow_id,
                        task_queue=queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    state = await client.get_workflow_handle(workflow_id).describe(
                        rpc_timeout=timedelta(seconds=3)
                    )
                    if state.status == WorkflowExecutionStatus.COMPLETED:
                        progress = await client.get_workflow_handle(workflow_id).query(
                            "graph_progress", result_type=dict, rpc_timeout=timedelta(seconds=3)
                        )
                        if progress["status"] == "completed":
                            await asyncio.to_thread(repo.recover_completed, job_id, store)
                        else:
                            await asyncio.to_thread(repo.fail, job_id)
                    elif state.status != WorkflowExecutionStatus.RUNNING:
                        await asyncio.to_thread(repo.fail, job_id)
        except Exception:
            logging.exception("Доставка графа будет повторена")
        if time.monotonic() >= next_reconcile:
            next_reconcile = time.monotonic() + 10
            try:
                await asyncio.to_thread(repo.reconcile, store)
            except Exception as error:
                logging.warning("Сверка графов отложена: %s", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass
