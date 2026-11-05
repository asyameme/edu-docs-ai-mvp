from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import SourceRef


class AttributeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any
    source_ref: SourceRef


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    entity_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(default_factory=list)

    attribute_sources: dict[str, list[AttributeEvidence]] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    target_id: str
    relation_type: str
    source_ref: SourceRef
    document_type: str
    semester: int | None = None
    value: int | None = None


class EducationalGraph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    @staticmethod
    def _add_evidence(
        node: GraphNode,
        key: str,
        evidence: AttributeEvidence,
    ) -> None:
        items = node.attribute_sources.setdefault(key, [])

        if evidence not in items:
            items.append(evidence.model_copy(deep=True))

    @classmethod
    def _ensure_evidence(cls, node: GraphNode) -> None:
        for key, value in node.attributes.items():
            if key in node.attribute_sources:
                continue

            if value is None:
                continue

            for ref in node.source_refs:
                cls._add_evidence(
                    node,
                    key,
                    AttributeEvidence(value=value, source_ref=ref),
                )

    def add_node(self, node: GraphNode) -> None:
        incoming = node.model_copy(deep=True)
        self._ensure_evidence(incoming)

        existing = next(
            (n for n in self.nodes if n.id == incoming.id),
            None,
        )

        if existing is None:
            self.nodes.append(incoming)
            return

        for ref in incoming.source_refs:
            if ref not in existing.source_refs:
                existing.source_refs.append(ref)

        for key, evidences in incoming.attribute_sources.items():
            for evidence in evidences:
                self._add_evidence(existing, key, evidence)

        for key, value in incoming.attributes.items():
            if key not in existing.attributes:
                existing.attributes[key] = value
            elif existing.attributes[key] is None and value is not None:
                existing.attributes[key] = value

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)
