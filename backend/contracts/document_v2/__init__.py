"""Общие специализированные модели результатов документов V2."""

from .models import (
    CharacteristicOutput,
    CKMVOutput,
    CurriculumOutput,
    MatrixOutput,
    RPDOutput,
    RPPOutput,
    parse_document,
)

__all__ = [
    "CharacteristicOutput",
    "CKMVOutput",
    "CurriculumOutput",
    "MatrixOutput",
    "RPDOutput",
    "RPPOutput",
    "parse_document",
]
