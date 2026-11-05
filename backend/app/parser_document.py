"""Чтение версий результата без привязки к реализации извлечения документов."""

from backend.contracts.document_v2.models import DocumentOutput, parse_document
from backend.contracts.parser_output import ParserOutput
from pydantic import BaseModel, ValidationError

ParserDocument = ParserOutput | DocumentOutput


def parse_output(value) -> ParserDocument:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
        raise ValueError("Результат документа должен содержать объект meta")
    version = value["meta"].get("schema_version")
    if "schema_version" not in value["meta"]:
        # Прежние V1 могут не передавать версию. Уникальные поля V2 распознаём
        # общей моделью команды, не добавляя метаданные в исходный JSON.
        try:
            return ParserOutput.model_validate(value)
        except ValidationError:
            version = 2
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Неподдерживаемая версия результата документа")
    # JSON Schema матрицы допускает целые 0/1; bool не является числом ячейки.
    # Проверка границы Backend не меняет общую модель команды.
    if version == 2 and value["meta"].get("doc_type") == "MATRIX":
        cells = value.get("matrix_cells", [])
        for cell in cells if isinstance(cells, list) else []:
            if isinstance(cell, dict) and type(cell.get("value")) is not int:
                raise ValueError("Ячейка матрицы должна содержать целое 0 или 1")
    return ParserOutput.model_validate(value) if version == 1 else parse_document(value)


def has_document_data(document: ParserDocument) -> bool:
    return any(
        value
        for key, value in document.model_dump(mode="json").items()
        if key not in {"meta", "parse_issues"}
    )
