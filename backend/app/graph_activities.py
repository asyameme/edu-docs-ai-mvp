"""Публикация Neo4j до SQL-отчёта; повтор после сбоя безопасен."""

from uuid import UUID

from pydantic import ValidationError
from temporalio import activity

from app.errors import DomainError
from app.graph_repository import GraphRepository


class GraphActivities:
    def __init__(self, pool, store):
        self.repo = GraphRepository(pool)
        self.store = store

    @activity.defn(name="graph_build_v1")
    def build(self, request):
        job = self.repo.job_by_id(UUID(request["job_id"]))
        if "outcome" in job["report"]:
            return job["report"]["outcome"]
        try:
            graph = self.repo.projection(job["program_id"], job["input_version_id"])
            if (
                graph["snapshot_hash"] != job["parameters"]["snapshot_hash"]
                or graph["converter_revision"] != job["parameters"]["converter_revision"]
            ):
                raise ValueError("Graph input changed")
            self.store.publish(graph)
            self.repo.finish(job, graph)
            return "completed"
        except (ValidationError, ValueError) as error:
            activity.logger.warning("Некорректный снимок графа: %s", type(error).__name__)
            self.repo.fail(job["id"], "graph_invalid_snapshot")
            return "failed"
        except DomainError as error:
            if error.status != 413:
                raise
            self.repo.fail(job["id"], "graph_invalid_snapshot")
            return "failed"

    @activity.defn(name="graph_failed_v1")
    def failed(self, request):
        self.repo.fail(UUID(request["job_id"]))
