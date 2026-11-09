"""Разобрать общую характеристику образовательной программы в DOCX."""

import argparse
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from backend.contracts.document_v2.models import (
    CharacteristicMeta,
    CharacteristicOutput,
    Competency,
    ParseIssue,
    ProgramV2,
    SourceRef,
)

from .common import finalize_output, normalize, paragraph_indices
from .results import split_competency


RESULTS_TITLE = "планируемые результаты освоения образовательной программы"
METADATA_LABELS = (
    ("level", r"уровень(?: образования)?"),
    ("program_cipher", r"шифр программы"),
    ("study_form", r"форм[аы](?:\(ы\))? обучения"),
    ("education_languages", r"язык(?:и|\(и\))? обучения"),
    ("study_duration", r"срок(?:и|\(и\))? обучения"),
)
LEVELS = {"бакалавриат", "магистратура", "специалитет", "аспирантура"}


def _section_label(text):
    return re.sub(
        r"^(?:раздел\s+)?\d+(?:\.\d+)*\.?\s+", "", normalize(text),
    ).rstrip(".:")


def _program_metadata(document, title_ref, paragraph_ref, issues):
    values = {}
    refs = {}
    ambiguous = set()
    direction_label = None
    table_number = 0

    def record(field, value, ref):
        if field in ambiguous:
            return
        if field in values and values[field] != value:
            issues.append(ParseIssue(
                category="ambiguous",
                message=f"В характеристике указаны разные значения поля {field}",
                source_ref=ref,
            ))
            values.pop(field)
            refs.pop(field)
            ambiguous.add(field)
            return
        values.setdefault(field, value)
        refs.setdefault(field, ref)

    for block in document.iter_inner_content():
        if isinstance(block, Table):
            table_number += 1
            rows = list(block.rows)
            if not rows:
                continue
            for col, header in enumerate(rows[0].cells, 1):
                label = normalize(header.text)
                if label == "наименование программы":
                    field = "name"
                elif label.startswith("направленность образовательной программы") and "англ" not in label:
                    field = "profile"
                else:
                    continue
                for row_number, row in enumerate(rows[1:], 2):
                    if col > len(row.cells) or row.grid_cols_before or row.grid_cols_after:
                        continue
                    value = " ".join(row.cells[col - 1].text.split())
                    if value:
                        record(field, value, SourceRef(
                            file=title_ref.file, section=header.text.strip(),
                            table=table_number, row=row_number, col=col,
                        ))
            continue

        text = " ".join(block.text.split())
        if not text:
            continue
        if _section_label(text) in {"аннотация", RESULTS_TITLE}:
            break
        ref = paragraph_ref(block, title_ref.section)
        if direction_label is not None:
            code = re.match(r"^\d{2}\.\d{2}\.\d{2}(?=\s|$)", text)
            if code:
                record("direction_code", code[0], ref)
                direction_label = None
                continue
            issues.append(ParseIssue(
                category="unexpected_format", message="Не прочитан код направления подготовки",
                source_ref=direction_label,
            ))
            direction_label = None
        if normalize(text).startswith("код, наименование направления подготовки"):
            direction_label = ref
            continue
        for field, label in METADATA_LABELS:
            match = re.fullmatch(rf"{label}(?:\s*:\s*|\s+)(.+)", text, re.IGNORECASE)
            if not match:
                continue
            value = match[1].strip()
            if field == "level":
                value = normalize(value)
                if value not in LEVELS:
                    issues.append(ParseIssue(
                        category="unexpected_format",
                        message=f"Не распознан уровень образования: {value}", source_ref=ref,
                    ))
                    break
            elif field == "education_languages":
                value = [lang.strip() for lang in re.split(r"[,;]|\s+и\s+", value) if lang.strip()]
            record(field, value, ref)
            break

        standard = re.search(r"\bобразовательным стандартом\s+(.+)", text, re.IGNORECASE)
        if standard:
            name = standard[1].rstrip(".")
            if normalize(name) == "санкт-петербургского государственного университета":
                name = "СПбГУ"
            record("education_standard", "Образовательный стандарт " + name, ref)

    if direction_label is not None:
        issues.append(ParseIssue(
            category="not_found", message="После заголовка отсутствует код направления",
            source_ref=direction_label,
        ))
    if "name" not in values and "name" not in ambiguous:
        issues.append(ParseIssue(
            category="not_found", message="Не найдено наименование программы", source_ref=title_ref,
        ))
    codes = (
        re.match(r"^Код\s+(\d{2}\.\d{3})\s+Профессиональный стандарт\b", " ".join(p.text.split()), re.IGNORECASE)
        for p in document.paragraphs
    )
    return ProgramV2(
        source_ref=refs.get("name", title_ref),
        professional_standards=list(dict.fromkeys(code[1] for code in codes if code)),
        **values,
    )


def _read_competencies(document, paragraph_ref, issues, title_ref):
    blocks = list(document.iter_inner_content())
    starts = [
        index for index, block in enumerate(blocks)
        if isinstance(block, Paragraph)
        and not block.style.name.casefold().startswith(("toc", "оглавление"))
        and _section_label(block.text) == RESULTS_TITLE
    ]
    if len(starts) != 1:
        issues.append(ParseIssue(
            category="not_found" if not starts else "ambiguous",
            message="Не найден однозначный раздел результатов освоения программы",
            source_ref=title_ref,
        ))
        return []
    section = blocks[starts[0]].text.strip()
    competencies = []
    table_number = sum(isinstance(block, Table) for block in blocks[:starts[0]])
    for block in blocks[starts[0] + 1:]:
        if not isinstance(block, Paragraph):
            table_number += 1
            issues.append(ParseIssue(
                category="manual_review", message="Таблица в разделе результатов требует разбора",
                source_ref=SourceRef(file=title_ref.file, section=section, table=table_number),
            ))
            continue
        label = _section_label(block.text)
        if label.startswith(("сопоставление компетенций", "описание обязательных требований")):
            break
        entry = split_competency(block.text)
        if entry is not None:
            code, formulation = entry
            ref = paragraph_ref(block, section)
            competencies.append(Competency(code=code, formulation=formulation, source_ref=ref))
            if not formulation:
                issues.append(ParseIssue(
                    category="empty_cell", message=f"У компетенции {code} отсутствует формулировка",
                    source_ref=ref,
                ))
    if not competencies:
        issues.append(ParseIssue(
            category="not_found", message="В разделе результатов не прочитаны компетенции",
            source_ref=paragraph_ref(blocks[starts[0]], section),
        ))
    return competencies


def parse_characteristic(file_path, *, program_id: str | None = None) -> CharacteristicOutput:
    file_path = Path(file_path)
    if file_path.suffix.lower() != ".docx":
        raise ValueError("Ожидается общая характеристика в формате DOCX")
    raw = file_path.read_bytes()
    document = Document(BytesIO(raw))
    title = next((
        p for p in document.paragraphs
        if re.match(r"^общая характеристика\b", normalize(p.text))
    ), None)
    if title is None:
        raise ValueError("Не удалось распознать общую характеристику")
    indices = paragraph_indices(document)

    def paragraph_ref(paragraph, section):
        return SourceRef(file=file_path.name, section=section, paragraph_index=indices[paragraph._p])

    title_ref = paragraph_ref(title, title.text.strip())
    issues = []
    program = _program_metadata(document, title_ref, paragraph_ref, issues)
    competencies = _read_competencies(document, paragraph_ref, issues, title_ref)
    output = CharacteristicOutput(
        meta=CharacteristicMeta(
            file_name=file_path.name, file_hash=sha256(raw).hexdigest(),
            parser_config="characteristic_docx_v1", program_id=program_id,
        ),
        program=program, competencies=competencies, parse_issues=issues,
    )
    return finalize_output(output, document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--program-id")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Выходной файл должен отличаться от исходного")
    try:
        output = parse_characteristic(args.input, program_id=args.program_id)
        args.output.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
