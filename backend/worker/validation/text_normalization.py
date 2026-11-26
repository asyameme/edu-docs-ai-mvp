"""Shared normalization for presentation-only text differences."""

import re
import unicodedata


def normalize_formulation_text(value: str) -> str:
    """Normalize Unicode, whitespace, case, and punctuation at text boundaries."""
    value = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    start, end = 0, len(value)
    while start < end and unicodedata.category(value[start]).startswith('P'):
        start += 1
    while end > start and unicodedata.category(value[end - 1]).startswith('P'):
        end -= 1
    return value[start:end].strip()


_INCLUDING_ABBREVIATION = re.compile(
    r'\bв\s+т\s*\.\s*ч\s*\.?(?=\s|$|[,;:])'
)
_LEVEL_NOTE_SUFFIX = re.compile(
    r"\s*(?P<level>\(\s*(?:базовый|средний|продвинутый|пороговый|повышенный)\s*\))"
    r"(?P<ending>[.!?]?)\s*$",
    re.IGNORECASE,
)


def format_variant_formulation_text(value: str) -> str:
    match = _LEVEL_NOTE_SUFFIX.search(value)
    if match is None:
        return value
    body = value[:match.start()].rstrip()
    if not body:
        return value
    if body[-1].isalnum() or body[-1] in "»\"')]}":
        body += "."
    return f"{body} {match['level']}{match['ending']}"


def normalize_variant_formulation_text(value: str) -> str:
    """Normalize presentation differences shared by cross-document variants.

    Уровень освоения («(Базовый)», «(Средний)», …) — часть формулировки:
    варианты, различающиеся только им, остаются вопросом для выбора
    пользователя, а не сворачиваются автоматически.
    """
    value = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    value = value.replace('ё', 'е')
    value = _INCLUDING_ABBREVIATION.sub('в том числе', value)
    value = format_variant_formulation_text(value)
    return normalize_formulation_text(value)
