"""Чтение версий результата без привязки к реализации извлечения документов."""

from pydantic import BaseModel

from .document_v2.models import DocumentOutput, parse_document
from .parser_output import ParserOutput

ParserDocument = ParserOutput | DocumentOutput


def parse_output(value) -> ParserDocument:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
        raise ValueError("Результат документа должен содержать объект meta")
    version = value["meta"].get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Неподдерживаемая версия результата документа")
    return ParserOutput.model_validate(value) if version == 1 else parse_document(value)


def has_document_data(document: ParserDocument) -> bool:
    return any(
        value
        for key, value in document.model_dump(mode="json").items()
        if key not in {"meta", "parse_issues"}
    )
