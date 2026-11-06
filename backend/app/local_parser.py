"""Вызов парсера V2 внутри Worker без отдельного сетевого сервиса."""

from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile

from backend.worker.parser.parse_curriculum import parse_curriculum
from backend.worker.parser.parse_rpd import parse_rpd
from backend.worker.parser.parse_rpp import parse_rpp
from docx.opc.exceptions import PackageNotFoundError


def parse_local_document(name: str, content: bytes, program_id) -> dict:
    """Разобрать поддерживаемый DOCX и вернуть исходный словарь V2."""
    filename = Path(name).name
    if Path(filename).suffix.lower() != ".docx":
        raise ValueError("XLSX пока не поддерживается локальным парсером V2")
    with TemporaryDirectory() as directory:
        path = Path(directory) / filename
        path.write_bytes(content)
        errors = []
        for parser in (parse_rpd, parse_rpp, parse_curriculum):
            try:
                return parser(path, program_id=str(program_id)).model_dump(mode="json")
            except (ValueError, PackageNotFoundError, BadZipFile) as error:
                errors.append(str(error))
    raise ValueError("Не удалось разобрать DOCX: " + "; ".join(errors))
