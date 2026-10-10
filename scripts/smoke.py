import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import httpx
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import get_settings
from app.workflows import DiagnosticInput, DiagnosticWorkflow


async def main() -> None:
    settings = get_settings()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=10, trust_env=False) as http:
        for path in ("/health/live", "/health/ready", "/api/v1/demo/program", "/openapi.json"):
            response = await http.get(path)
            response.raise_for_status()
            if path.endswith("program"):
                assert response.json()["is_demo"] is True
            if path.endswith("ready"):
                assert response.json()["dependencies"]["temporal"] == "available"
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    marker = uuid4().hex
    handle = await client.start_workflow(
        DiagnosticWorkflow.run,
        DiagnosticInput(marker),
        id="diagnostic-" + marker,
        task_queue=settings.temporal_task_queue,
        execution_timeout=timedelta(minutes=10),
    )
    result = await asyncio.wait_for(handle.result(), timeout=40)
    assert result["marker"] == marker
    assert result["before"]["marker"] == result["after"]["marker"] == marker
    await Replayer(workflows=[DiagnosticWorkflow]).replay_workflow(await handle.fetch_history())
    print(json.dumps({"http": "passed", "temporal": "passed", "replay": "passed", **result}))


if __name__ == "__main__":
    asyncio.run(main())
