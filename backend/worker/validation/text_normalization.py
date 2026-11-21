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


def normalize_variant_formulation_text(value: str) -> str:
    """Normalize presentation differences shared by cross-document variants."""
    value = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    value = value.replace('ё', 'е')
    value = _INCLUDING_ABBREVIATION.sub('в том числе', value)
    return normalize_formulation_text(value)
