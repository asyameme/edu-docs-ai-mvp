from typing import Literal

from pydantic import BaseModel


class LiveResponse(BaseModel):
    status: Literal["alive"] = "alive"


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: dict[str, Literal["available", "unavailable"]]


class ProgramCard(BaseModel):
    id: str
    name: str


class VersionCard(BaseModel):
    id: str
    number: int
    status: Literal["draft"]


class DisciplineCard(BaseModel):
    id: str
    name: str
    semesters: list[int]
    hours: int | None


class CompetencyCard(BaseModel):
    id: str
    code: str
    name: str
    type: str


class SourceRef(BaseModel):
    id: str
    document_id: str
    section: str
    locator: str


class RelationCard(BaseModel):
    source_id: str
    target_id: str
    type: str
    source_ref_ids: list[str]


class ConflictOption(BaseModel):
    value: int
    source_ref_id: str


class ConflictCard(BaseModel):
    id: str
    entity_id: str
    field: str
    status: Literal["unresolved"]
    options: list[ConflictOption]


class DemoProgram(BaseModel):
    is_demo: Literal[True] = True
    notice: str
    program: ProgramCard
    version: VersionCard
    disciplines: list[DisciplineCard]
    competencies: list[CompetencyCard]
    source_refs: list[SourceRef]
    relations: list[RelationCard]
    conflicts: list[ConflictCard]
