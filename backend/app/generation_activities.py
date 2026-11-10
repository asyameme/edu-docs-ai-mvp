"""Фоновая сборка ZIP из подтверждённой версии и сохранённых оригиналов."""

from pathlib import PurePosixPath
from uuid import UUID

from temporalio import activity

from app.errors import DomainError
from app.generation_bridge import (
    GenerationBridgeError,
    approved_changes,
    snapshot_matrix_graph,
)
from app.generation_repository import GenerationRepository
from worker.generation import (
    DocxPatchError,
    FieldEvidence,
    GenerationMappingError,
    GenerationPackageError,
    OriginalDocument,
    SourceEvidence,
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
        snapshot, sources, rows = data
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
            links = [
                FieldEvidence(
                    item.target_kind,
                    str(item.target_id),
                    item.field_path,
                    str(item.source_ref_id),
                    item.observed_value,
                )
                for item in snapshot.source_links
            ]
            refs = [
                SourceEvidence(str(item.id), str(item.document_id), item.locator)
                for item in snapshot.source_refs
            ]
            documents = build_generation_documents(
                approved_changes(snapshot), links, refs, originals
            )
            content = generate_program_package(documents, snapshot_matrix_graph(snapshot))
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
        return self.repo.finish(job_id, content)

    @activity.defn(name="generation_failed_v1")
    def failed(self, request: dict) -> None:
        self.repo.fail(UUID(request["job_id"]), "Генерация не завершилась после повторных попыток")
