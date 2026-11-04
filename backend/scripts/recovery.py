"""Сохранение таймера и проверка продолжения процесса после внешнего перезапуска."""

import asyncio
import json
import sys
from datetime import timedelta
from uuid import uuid4

from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import get_settings
from app.workflows import DiagnosticInput, DiagnosticWorkflow


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    action = sys.argv[1]
    if action == "prepare":
        marker = uuid4().hex
        handle = await client.start_workflow(
            DiagnosticWorkflow.run,
            DiagnosticInput(marker, delay_seconds=90),
            id="recovery-" + marker,
            task_queue=settings.temporal_task_queue,
            execution_timeout=timedelta(minutes=10),
        )
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            history = await handle.fetch_history()
            completed = [
                event
                for event in history.events
                if event.HasField("activity_task_completed_event_attributes")
            ]
            if len(completed) == 1 and any(
                event.HasField("timer_started_event_attributes") for event in history.events
            ):
                print(
                    json.dumps(
                        {
                            "workflow_id": handle.id,
                            "run_id": handle.first_execution_run_id,
                            "marker": marker,
                            "first_event_id": completed[0].event_id,
                        }
                    )
                )
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("Durable timer checkpoint not reached")
    if action != "verify":
        raise ValueError("Expected prepare or verify")
    state = json.loads(sys.stdin.read().lstrip("\ufeff"))
    handle = client.get_workflow_handle(state["workflow_id"], run_id=state["run_id"])
    result = await asyncio.wait_for(handle.result(), timeout=180)
    assert result["marker"] == state["marker"]
    assert result["before"]["marker"] == result["after"]["marker"] == state["marker"]
    assert result["before"]["session"] != result["after"]["session"]
    history = await handle.fetch_history()
    completed = [
        event
        for event in history.events
        if event.HasField("activity_task_completed_event_attributes")
    ]
    assert len(completed) == 2
    assert completed[0].event_id == state["first_event_id"]
    await Replayer(workflows=[DiagnosticWorkflow]).replay_workflow(history)
    print(
        json.dumps({"recovery": "passed", "completed_activities": 2, "replay": "passed", **result})
    )


if __name__ == "__main__":
    asyncio.run(main())
