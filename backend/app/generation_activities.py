"""Фоновая сборка ZIP из подтверждённой версии и сохранённых оригиналов."""

from pathlib import PurePosixPath
from uuid import UUID

from temporalio import activity

from app.errors import DomainError
from app.generation_bridge import (
    GenerationBridgeError,
    apply_competency_list_edits,
    apply_shared_indicator_edits,
    describe_unresolved,
    shared_indicator_edits,
    snapshot_matrix_graph,
    snapshot_plan,
    unresolved_matrix_pairs,
)
from app.generation_repository import GenerationRepository
from worker.generation import (
    DocxPatchError,
    FieldEvidence,
    GenerationMappingError,
    GenerationPackageError,
    OriginalDocument,
    SourceEvidence,
    UnresolvedPatch,
    build_generation_documents,
    generate_program_package,
)

MAX_PACKAGE_BYTES = 500 * 1024 * 1024


class GenerationActivities:
    def __init__(self, pool):
        self.repo = GenerationRepository(pool)

    @activity.defn(name="generation_build_v1")
    def build(self, request: dict) -> str:
        job_id = UUID(request["job_id"])
        data = self.repo.input(job_id)
        if isinstance(data, str):
            return data
        snapshot, sources, rows, parsed_documents = data
        notes: list[str] = []
        applied: list[str] = []
        try:
            source_by_id = {item["document_id"]: item for item in sources}
            originals = []
            for row in rows:
                identity = str(row["id"])
                source = source_by_id.get(identity)
                if not source or row["format"] != "docx":
                    raise GenerationBridgeError("Нет разобранного DOCX для одного из источников")
                if source["file_hash"] != row["sha256"]:
                    raise GenerationBridgeError("Оригинал и результат парсера имеют разные хэши")
                kind = source["doc_type"]
                if kind not in {"RPD", "RPP", "CURRICULUM", "CKMV", "CHARACTERISTIC"}:
                    raise GenerationBridgeError("Тип документа не поддержан генератором")
                name = "_".join(PurePosixPath(source["member_name"]).parts)
                originals.append(
                    OriginalDocument(identity, kind, name, bytes(row["content"]))
                )
            shared = shared_indicator_edits(snapshot, parsed_documents)
            replaced = {location for edit in shared for location in edit.replaced_links}
            links = [
                FieldEvidence(
                    item.target_kind,
                    str(item.target_id),
                    item.field_path,
                    str(item.source_ref_id),
                    item.observed_value,
                )
                for item in snapshot.source_links
                if item.field_path != "/attributes/text"
                or (str(item.target_id), str(item.source_ref_id)) not in replaced
            ]
            refs = [
                SourceEvidence(str(item.id), str(item.document_id), item.locator)
                for item in snapshot.source_refs
            ]
            plan = snapshot_plan(snapshot, parsed_documents)
            has_remaining_evidence = {
                (item.target_kind, item.target_id, item.field_path) for item in links
            }
            split_targets = {target for target, _ in replaced}
            changes = [
                change for change in plan.changes
                if change.field_path != "/attributes/text" or change.target_id not in split_targets
                or (
                    change.target_kind, change.target_id, change.field_path
                ) in has_remaining_evidence
            ]
            unresolved: list[UnresolvedPatch] = []
            documents = build_generation_documents(
                changes, links, refs, originals, unresolved
            )
            notes.extend(describe_unresolved(snapshot, documents, unresolved))
            # Решение о списке компетенций, которое нельзя записать в документ,
            # попадает в отчёт генерации, а не блокирует весь пакет.
            documents = apply_competency_list_edits(
                documents, plan.competency_lists, parsed_documents, notes
            )
            documents = apply_shared_indicator_edits(documents, shared, notes)
            unresolved_source = snapshot.metadata.get("unresolved_relations", [])
            if unresolved_source:
                notes.append(
                    "В исходных документах осталось неразрешённых связей: "
                    f"{len(unresolved_source)}. "
                    "В производной матрице знак ? означает неопределённость, а не отсутствие связи."
                )
            content = generate_program_package(
                documents, snapshot_matrix_graph(snapshot), notes=notes, applied=applied,
                unresolved_pairs=unresolved_matrix_pairs(snapshot),
            )
            if len(content) > MAX_PACKAGE_BYTES:
                raise GenerationBridgeError("Сгенерированный ZIP превышает допустимый размер")
        except (
            GenerationBridgeError,
            GenerationMappingError,
            GenerationPackageError,
            DocxPatchError,
            DomainError,
            ValueError,
            KeyError,
        ) as error:
            self.repo.fail(job_id, str(error))
            return "failed"
        return self.repo.finish(
            job_id, content, notes=notes, applied=applied
        )

    @activity.defn(name="generation_failed_v1")
    def failed(self, request: dict) -> None:
        self.repo.fail(UUID(request["job_id"]), "Генерация не завершилась после повторных попыток")
