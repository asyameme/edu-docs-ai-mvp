"""Разбор программ практик с сохранением источников и проблем извлечения."""

import argparse
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from .assessment import extract_assessment_tools, extract_practice_score_scale
from .common import finalize_output, heading_level, normalize
from .content import extract_course_content
from .discipline import (
    build_practice_discipline,
    extract_practice_control_forms,
    extract_prerequisites,
    read_title_details,
)
from backend.contracts.document_v2.models import ParseIssue, RPPMeta, RPPOutput, SourceRef
from .results import extract_practice_competencies, extract_results_into


def _results_roots(document):
    """Найти внешний раздел компетенций, исключая оглавление."""
    candidates = []
    for paragraph in document.paragraphs:
        if paragraph.style.name.casefold().startswith(("toc", "оглавление")):
            continue
        if re.search(r"\t\s*\d+\s*$", paragraph.text):
            continue
        text = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*", "", normalize(paragraph.text),
        ).rstrip(".:")
        if text == (
            "перечень профессиональных компетенций, "
            "формирующих практическую составляющую "
            "результатов освоения программы"
        ):
            candidates.append(paragraph)

    depth = min((heading_level(p) or 2 for p in candidates), default=2)
    return {
        p._p for p in candidates
        if (heading_level(p) or 2) == depth
    }


def finalize_rpp(output, document, file_name):
    """Добавить содержание, формы контроля и оценочные материалы."""
    discipline = output.disciplines[0]

    forms, issues = extract_practice_control_forms(document, file_name)
    output.parse_issues.extend(issues)
    discipline.control_forms = forms

    prerequisites, issues = extract_prerequisites(
        document, file_name, doc_type="RPP",
    )
    output.parse_issues.extend(issues)
    discipline.prerequisites = [item["name"] for item in prerequisites]

    topics, issues = extract_course_content(
        document,
        file_name,
        semesters=discipline.semesters or None,
        doc_type="RPP",
    )
    output.parse_issues.extend(issues)
    output.course_content.extend(topics)

    tools, relations, issues = extract_assessment_tools(
        document, file_name, output.indicators, doc_type="RPP",
    )
    output.assessment_tools.extend(tools)
    output.relations.extend(relations)
    output.parse_issues.extend(issues)

    scale, issues = extract_practice_score_scale(document, file_name)
    output.score_scale.extend(scale)
    output.parse_issues.extend(issues)
    return finalize_output(output, document)


def parse_rpp(file_path, *, program_id: str | None = None) -> RPPOutput:
    file_path = Path(file_path)
    if file_path.suffix.lower() != ".docx":
        raise ValueError("Ожидается файл DOCX")

    raw = file_path.read_bytes()
    document = Document(BytesIO(raw))
    title_parts = []
    title_blocks = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            if heading_level(block) is not None:
                break
            title_parts.append(block.text)
        elif isinstance(block, Table):
            title_parts.extend(
                cell.text for row in block.rows for cell in row.cells
            )
        title_blocks.append(block)

    title = re.sub(r"\s+", "", "".join(title_parts)).casefold()
    if (
        "рабочаяпрограммапрактики" not in title
        or "рабочаяпрограммаучебнойдисциплины" in title
    ):
        raise ValueError("Не удалось однозначно распознать РПП по титулу")

    discipline, semester_workload, issues = build_practice_discipline(
        document, file_path.name, title_parts,
    )
    if discipline is None:
        details = "; ".join(issue.message for issue in issues)
        raise ValueError(
            f"Не удалось собрать практику. {details}".strip()
        )

    output = RPPOutput(
        meta=RPPMeta(
            file_name=file_path.name,
            file_hash=sha256(raw).hexdigest(),
            parser_config="rpp_docx_v4",
            program_id=program_id,
        ),
        disciplines=[discipline],
        semester_workload=semester_workload,
        parse_issues=issues,
    )
    read_title_details(document, title_blocks, output)
    output.parse_issues.append(ParseIssue(
        category="manual_review",
        severity="info",
        message=(
            "Разбор РПП поддерживает общую и посеместровую нагрузку, перечни и таблицы "
            "компетенций, результаты обучения, табличное содержание, "
            "формы контроля, оценочные средства и шкалы. "
            "Текстовые этапы требуют отдельного разбора."
        ),
        source_ref=SourceRef(file=file_path.name),
    ))

    roots = _results_roots(document)

    def is_heading(paragraph):
        return paragraph._p in roots

    output = extract_results_into(
        output, document, file_path.name, is_heading,
    )
    # При отсутствии/неоднозначности раздела общий разбор уже добавил проблему.
    if len(roots) == 1:
        output = extract_practice_competencies(
            output, document, file_path.name, is_heading,
        )
    return finalize_rpp(output, document, file_path.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--program-id")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Выходной файл должен отличаться от исходного")

    try:
        output = parse_rpp(args.input, program_id=args.program_id)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    text = output.model_dump_json(indent=2)
    RPPOutput.model_validate_json(text)
    args.output.write_text(text, encoding="utf-8")
    print(
        f"Практик: {len(output.disciplines)}. "
        f"Компетенций: {len(output.competencies)}. "
        f"Индикаторов: {len(output.indicators)}. "
        f"Результатов обучения: {len(output.learning_outcomes)}. "
        f"Оценочных средств: {len(output.assessment_tools)}. "
        f"Строк шкалы: {len(output.score_scale)}."
    )


if __name__ == "__main__":
    main()
