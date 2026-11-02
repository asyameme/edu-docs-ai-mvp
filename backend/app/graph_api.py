"""Просмотр точного графа версии и её источников, без произвольных Cypher-запросов."""

import asyncio
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from temporalio.client import WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

from app.errors import DomainError
from app.graph_repository import GraphRepository
from app.program_api import Limit, Offset, OperationKey, RepoDep

router = APIRouter(prefix="/api/v1/programs/{program_id}", tags=["Graphs"])


def graph_store(request: Request):
    return request.app.state.resources.graph


GraphDep = Annotated[object, Depends(graph_store)]


class GraphJobRead(BaseModel):
    id: UUID
    input_version_id: UUID
    output_version_id: UUID | None
    status: Literal["pending", "processing", "completed", "failed"]
    progress: int
    report: dict


def job_result(row, status=None, progress=None):
    outcome = row["report"].get("outcome")
    return {
        "id": row["id"],
        "input_version_id": row["input_version_id"],
        "output_version_id": row["output_version_id"],
        "report": row["report"],
        "status": outcome or status or "pending",
        "progress": 100 if outcome else (progress or 0),
    }


@router.post("/versions/{version_id}/graph/builds", status_code=202)
def build_graph(
    program_id: UUID, version_id: UUID, operation_key: OperationKey, repo: RepoDep
) -> GraphJobRead:
    graphs = GraphRepository(repo.pool)
    identity = graphs.create(program_id, version_id, operation_key)
    return job_result(graphs.job(program_id, identity))


@router.get("/graph-jobs/{job_id}")
async def graph_job(
    program_id: UUID, job_id: UUID, repo: RepoDep, request: Request
) -> GraphJobRead:
    graphs = GraphRepository(repo.pool)
    row = await asyncio.to_thread(graphs.job, program_id, job_id)
    if "outcome" in row["report"]:
        return job_result(row)
    client = request.app.state.resources.temporal
    if client.namespace != row["temporal_namespace"]:
        raise DomainError(503, "Пространство имён графовой задачи недоступно")
    try:
        handle = client.get_workflow_handle(row["workflow_id"])
        description = await handle.describe(rpc_timeout=timedelta(seconds=3))
        if description.status not in {
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.COMPLETED,
        }:
            return job_result(row, "failed", 100)
        state = await handle.query(
            "graph_progress", result_type=dict, rpc_timeout=timedelta(seconds=3)
        )
        row = await asyncio.to_thread(graphs.job, program_id, job_id)
        status = "processing" if state["status"] == "completed" else state["status"]
        return job_result(row, status, min(99, state["progress"]))
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return job_result(row)
        raise DomainError(503, "Прогресс графа временно недоступен") from error
    except (TimeoutError, WorkflowQueryFailedError) as error:
        raise DomainError(503, "Прогресс графа временно недоступен") from error


def read_graph(repo, store, program_id, version_id):
    graph = GraphRepository(repo.pool).projection(program_id, version_id)
    return store.read(graph)


@router.get("/versions/{version_id}/graph")
def graph_info(program_id: UUID, version_id: UUID, repo: RepoDep, store: GraphDep) -> dict:
    return {"ready": True, **read_graph(repo, store, program_id, version_id)["metadata"]}


@router.get("/versions/{version_id}/graph/entities")
def entities(
    program_id: UUID,
    version_id: UUID,
    repo: RepoDep,
    store: GraphDep,
    limit: Limit = 50,
    offset: Offset = 0,
    kind: str | None = None,
) -> dict:
    rows = read_graph(repo, store, program_id, version_id)["entities"]
    rows = [item for item in rows if kind is None or item["kind"] == kind]
    return {"total": len(rows), "items": rows[offset : offset + limit]}


@router.get("/versions/{version_id}/graph/relations")
def relations(
    program_id: UUID,
    version_id: UUID,
    repo: RepoDep,
    store: GraphDep,
    limit: Limit = 50,
    offset: Offset = 0,
    relation_type: str | None = None,
) -> dict:
    rows = read_graph(repo, store, program_id, version_id)["relations"]
    rows = [item for item in rows if relation_type is None or item["type"] == relation_type]
    return {"total": len(rows), "items": rows[offset : offset + limit]}


@router.get("/versions/{version_id}/graph/entities/{entity_id}")
def entity(
    program_id: UUID, version_id: UUID, entity_id: UUID, repo: RepoDep, store: GraphDep
) -> dict:
    data = read_graph(repo, store, program_id, version_id)
    item = next((item for item in data["entities"] if item["id"] == str(entity_id)), None)
    if item is None:
        raise DomainError(404, "Сущность отсутствует в этой версии графа")
    return {"entity": item, **evidence(repo, program_id, version_id, "entity", entity_id, item)}


def evidence(repo, program_id, version_id, kind, identity, item):
    snapshot = repo.version(program_id, version_id)["snapshot"]
    refs = set(item["source_ref_ids"])
    links = [
        link
        for link in snapshot.get("source_links", [])
        if link["target_kind"] == kind and link["target_id"] == str(identity)
    ]
    refs.update(link["source_ref_id"] for link in links)
    return {
        "version_id": version_id,
        "sources": [source for source in snapshot["source_refs"] if source["id"] in refs],
        "observations": links,
    }


@router.get("/versions/{version_id}/graph/relations/{relation_id}")
def relation(
    program_id: UUID, version_id: UUID, relation_id: UUID, repo: RepoDep, store: GraphDep
) -> dict:
    data = read_graph(repo, store, program_id, version_id)
    item = next((item for item in data["relations"] if item["id"] == str(relation_id)), None)
    if item is None:
        raise DomainError(404, "Связь отсутствует в этой версии графа")
    return {
        "relation": item,
        **evidence(repo, program_id, version_id, "relation", relation_id, item),
    }


@router.get("/versions/{version_id}/graph/entities/{entity_id}/neighbors")
def neighbors(
    program_id: UUID,
    version_id: UUID,
    entity_id: UUID,
    repo: RepoDep,
    store: GraphDep,
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
    direction: Literal["out", "in", "both"] = "out",
    relation_type: str | None = None,
    include_zero: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
) -> dict:
    graph = GraphRepository(repo.pool).projection(program_id, version_id)
    if str(entity_id) not in {item["props"]["entity_id"] for item in graph["nodes"]}:
        raise DomainError(404, "Сущность отсутствует в этой версии")
    return store.neighbors(
        graph, str(entity_id), depth, direction, relation_type, include_zero, limit, offset
    )
