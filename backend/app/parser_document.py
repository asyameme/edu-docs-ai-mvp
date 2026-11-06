"""Чтение результата V2 без привязки к реализации извлечения документов."""

from backend.contracts.document_v2.models import DocumentOutput, parse_document
from pydantic import BaseModel

ParserDocument = DocumentOutput


def parse_output(value) -> ParserDocument:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
        raise ValueError("Результат документа должен содержать объект meta")
    # Общие модели V2 допускают отсутствие версии и по умолчанию задают 2.
    version = value["meta"].get("schema_version", 2)
    if type(version) is not int or version != 2:
        raise ValueError("Backend принимает только результат документа V2")
    # JSON Schema матрицы допускает целые 0/1; bool не является числом ячейки.
    # Проверка границы Backend не меняет общую модель команды.
    if value["meta"].get("doc_type") == "MATRIX":
        cells = value.get("matrix_cells", [])
        for cell in cells if isinstance(cells, list) else []:
            if isinstance(cell, dict) and type(cell.get("value")) is not int:
                raise ValueError("Ячейка матрицы должна содержать целое 0 или 1")
    return parse_document(value)


def has_document_data(document: ParserDocument) -> bool:
    return any(
        value
        for key, value in document.model_dump(mode="json").items()
        if key not in {"meta", "parse_issues"}
    )
