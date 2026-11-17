"""Разобрать таблицы ролей, компетенций и результатов обучения в ЦКМВ DOCX."""

import argparse
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document

from backend.contracts.document_v2.models import (
    CKMVMeta,
    CKMVOutput,
    Competency,
    Indicator,
    IndicatorOutcomeLink,
    LearningOutcome,
    ParseIssue,
    ProfessionalRole,
    Relation,
    RoleCompetencyLink,
    SourceRef,
)

from .common import finalize_output, normalize, paragraph_indices
from .results import split_competency


INDICATOR_CODE = r"[А-ЯA-Z][А-ЯA-Z0-9]*(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+"
OUTCOME_PREFIX = re.compile(rf"^({INDICATOR_CODE})\.?\s+([ЗУВ]-\d+)[.)]?(?:\s+|$)")
OUTCOME_KINDS = {"З": "знать", "У": "уметь", "В": "владеть"}
ROLE_HEADINGS = {
    "роль",
    "основная профессиональная роль",
    "профессиональная роль",
    "профессиональная опережающая роль",
    "универсальные компетенции",
}


def _header(values):
    return (
        len(values) == 4
        and "компетенц" in normalize(values[1])
        and normalize(values[2]).startswith("индикатор")
        and re.match(r"результаты? обучения\b", normalize(values[3])) is not None
    )


def _issue(output, category, message, ref):
    output.parse_issues.append(ParseIssue(category=category, message=message, source_ref=ref))


def _read_competency(output, text, ref, role):
    entry = split_competency(text)
    if entry is None:
        _issue(output, "unparsed_row", f"Не распознана компетенция: {text}", ref)
        return None
    code, formulation = entry
    if not formulation:
        _issue(output, "empty_cell", f"У компетенции {code} отсутствует формулировка", ref)
    competency = Competency(code=code, formulation=formulation, source_ref=ref)
    output.competencies.append(competency)
    if role is not None:
        output.role_competency_links.append(RoleCompetencyLink(
            role_id=role.local_id, competency_code=code, source_ref=ref,
        ))
    return competency


def _read_indicator(output, text, ref, competency):
    match = re.fullmatch(rf"({INDICATOR_CODE})\.?\s+(.+)", text)
    if match is None or competency is None:
        _issue(output, "unparsed_row", f"Не распознан индикатор с компетенцией: {text}", ref)
        return None
    code, description = match.groups()
    if not code.startswith(competency.code + "."):
        _issue(
            output, "unexpected_format",
            f"Код индикатора {code} расходится с кодом компетенции {competency.code} в таблице",
            ref,
        )
    indicator = Indicator(
        code=code, text=description, competency_code=competency.code, source_ref=ref,
    )
    output.indicators.append(indicator)
    output.relations.append(Relation(
        src_entity="competency", src_key=competency.code,
        dst_entity="indicator", dst_key=code, kind="имеет", source_ref=ref,
    ))
    return indicator


def _read_outcome(output, text, ref, competency, indicator):
    prefix = OUTCOME_PREFIX.match(text)
    outcome = LearningOutcome(
        local_id=f"LO-{len(output.learning_outcomes) + 1:03}",
        text=text,
        kind=OUTCOME_KINDS[prefix[2][0]] if prefix else None,
        code=text[:prefix.end(2)] if prefix else None,
        source_ref=ref,
    )
    output.learning_outcomes.append(outcome)
    if competency is not None:
        output.relations.append(Relation(
            src_entity="competency", src_key=competency.code,
            dst_entity="learning_outcome", dst_key=outcome.local_id,
            kind="включает", source_ref=ref,
        ))
    if prefix is not None and indicator is not None and prefix[1] == indicator.code:
        output.indicator_outcome_links.append(IndicatorOutcomeLink(
            indicator_code=indicator.code, learning_outcome_id=outcome.local_id, source_ref=ref,
        ))
    else:
        _issue(
            output, "unparsed_row",
            f"Не удалось связать результат с индикатором текущей группы: {text}", ref,
        )


def _outcome_entries(cell):
    lines = []
    start = None
    for paragraph in cell.paragraphs:
        for line in paragraph.text.splitlines():
            text = " ".join(line.split())
            if not text:
                continue
            if lines and OUTCOME_PREFIX.match(text):
                yield " ".join(lines), start
                lines = []
            if not lines:
                start = paragraph
            lines.append(text)
    if lines:
        yield " ".join(lines), start


def _read_table(output, rows, table_number, header_row, roles, indices):
    file_name = output.meta.file_name
    section = None
    role = competency = indicator = None
    blank_start = None
    seen_cells = set()

    def source(row, col=None):
        return SourceRef(file=file_name, section=section, table=table_number, row=row, col=col)

    def finish_blank(end):
        nonlocal blank_start, competency, indicator
        if blank_start is not None and end > blank_start:
            _issue(
                output, "empty_cell", f"Пустой блок строк ЦКМВ: {blank_start}-{end}",
                source(blank_start),
            )
            competency = indicator = None
        blank_start = None

    for number, row in enumerate(rows[header_row - 1:], header_row):
        cells = row.cells
        if len(cells) != 4 or row.grid_cols_before or row.grid_cols_after:
            finish_blank(number - 1)
            _issue(output, "unexpected_format", "В строке ЦКМВ ожидаются четыре колонки", source(number))
            competency = indicator = None
            continue

        values = [
            "" if cell._tc in seen_cells else " ".join(cell.text.split())
            for cell in cells
        ]
        seen_cells.update(cell._tc for cell in cells)
        if not any(values):
            if any(cell.text.strip() for cell in cells):
                # Продолжение объединенной ячейки, а не пропуск в источнике.
                finish_blank(number - 1)
                continue
            if blank_start is None:
                blank_start = number
            continue

        finish_blank(number - 1)

        role_name, competency_text, indicator_text, outcome_text = values
        heading = normalize(role_name).rstrip(":")
        is_header = _header(values)
        if heading in ROLE_HEADINGS:
            role = competency = indicator = None
            section = role_name.rstrip(":")
            role_name = ""
        if is_header:
            section = competency_text
            competency = indicator = None
        if role_name:
            if role_name not in roles:
                roles[role_name] = ProfessionalRole(
                    local_id=f"ROLE-{len(roles) + 1:03}", name=role_name,
                    source_ref=source(number, 1),
                )
                output.professional_roles.append(roles[role_name])
            role = roles[role_name]
            competency = indicator = None
        if is_header:
            continue
        if competency_text:
            competency = _read_competency(output, competency_text, source(number, 2), role)
            indicator = None
        if indicator_text:
            indicator = _read_indicator(output, indicator_text, source(number, 3), competency)
        if outcome_text:
            for text, paragraph in _outcome_entries(cells[3]):
                ref = source(number, 4)
                ref.paragraph_index = indices[paragraph._p]
                _read_outcome(output, text, ref, competency, indicator)
    finish_blank(len(rows))


def parse_ckmv(file_path, *, program_id: str | None = None) -> CKMVOutput:
    file_path = Path(file_path)
    if file_path.suffix.lower() != ".docx":
        raise ValueError("Ожидается ЦКМВ в формате DOCX")
    raw = file_path.read_bytes()
    document = Document(BytesIO(raw))
    output = CKMVOutput(meta=CKMVMeta(
        file_name=file_path.name, file_hash=sha256(raw).hexdigest(),
        parser_config="ckmv_docx_v1", program_id=program_id,
    ))

    roles = {}
    found = False
    indices = paragraph_indices(document)
    for table_number, table in enumerate(document.tables, 1):
        rows = list(table.rows)
        header_row = next((
            number for number, row in enumerate(rows, 1)
            if _header([cell.text for cell in row.cells])
        ), None)
        if header_row is not None:
            found = True
            _read_table(output, rows, table_number, header_row, roles, indices)

    if not found:
        has_title = any(
            normalize(p.text).startswith("перечень выбранных профессиональных ролей")
            for p in document.paragraphs
        )
        if not has_title:
            raise ValueError("Не удалось распознать ЦКМВ")
        _issue(output, "not_found", "Не найдена таблица ЦКМВ", SourceRef(file=file_path.name))
    elif not output.competencies:
        _issue(output, "not_found", "В таблицах ЦКМВ нет прочитанных компетенций", SourceRef(file=file_path.name))
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
        output = parse_ckmv(args.input, program_id=args.program_id)
        args.output.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
