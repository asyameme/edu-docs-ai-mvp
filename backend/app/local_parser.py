"""Вызов парсера V2 внутри Worker без отдельного сетевого сервиса."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unicodedata import normalize
from zipfile import BadZipFile

from backend.worker.parser.parse_characteristic import parse_characteristic
from backend.worker.parser.parse_ckmv import parse_ckmv
from backend.worker.parser.parse_curriculum import parse_curriculum
from backend.worker.parser.parse_rpd import parse_rpd
from backend.worker.parser.parse_rpp import parse_rpp
from docx.opc.exceptions import PackageNotFoundError

PARSERS_BY_FOLDER = {
    "рпд": parse_rpd,
    "рпп": parse_rpp,
    "учебный план": parse_curriculum,
    "характеристика": parse_characteristic,
    "цкмв": parse_ckmv,
}


def parse_local_document(name: str, content: bytes, program_id) -> dict:
    """Выбрать парсер по папке ZIP и вернуть исходный словарь V2."""
    filename = Path(name).name
    if Path(filename).suffix.lower() != ".docx":
        raise ValueError("XLSX пока не поддерживается локальным парсером V2")
    folder = None
    for part in reversed(Path(name).parts[:-1]):
        key = normalize("NFC", part).casefold()
        if key in PARSERS_BY_FOLDER:
            folder = key
            break
    parsers = (
        (PARSERS_BY_FOLDER[folder],)
        if folder in PARSERS_BY_FOLDER
        else (parse_rpd, parse_rpp, parse_curriculum, parse_characteristic, parse_ckmv)
    )
    with TemporaryDirectory() as directory:
        path = Path(directory) / filename
        path.write_bytes(content)
        errors = []
        for parser in parsers:
            try:
                return parser(path, program_id=str(program_id)).model_dump(mode="json")
            except (ValueError, PackageNotFoundError, BadZipFile) as error:
                errors.append(str(error))
    raise ValueError("Не удалось разобрать DOCX: " + "; ".join(errors))
