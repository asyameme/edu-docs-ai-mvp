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
from .common import heading_level, normalize
from .content import extract_course_content
from .discipline import (
    build_practice_discipline,
    extract_practice_control_forms,
    extract_prerequisites,
)
from .models import ParseIssue, ParserMeta, ParserOutput, SourceRef
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
    discipline = output.disciplines[0] if output.disciplines else None

    forms, issues = extract_practice_control_forms(document, file_name)
    output.parse_issues.extend(issues)
    if discipline is not None:
        discipline.control_forms = forms
    elif forms:
        output.parse_issues.append(ParseIssue(
            category="manual_review",
            message=(
                "Формы контроля прочитаны, но практика с нагрузкой "
                "не создана: " + "; ".join(forms)
            ),
            source_ref=SourceRef(file=file_name),
        ))

    prerequisites, issues = extract_prerequisites(
        document, file_name, doc_type="RPP",
    )
    output.parse_issues.extend(issues)
    if discipline is not None:
        discipline.prerequisites = [item["name"] for item in prerequisites]
    elif prerequisites:
        output.parse_issues.append(ParseIssue(
            category="manual_review",
            message=(
                "Пререквизиты прочитаны, но практика с нагрузкой не создана: "
                + "; ".join(item["name"] for item in prerequisites)
            ),
            source_ref=prerequisites[0]["source_ref"],
        ))

    topics, issues = extract_course_content(
        document,
        file_name,
        semesters=discipline.semesters if discipline is not None else None,
        doc_type="RPP",
    )
    output.parse_issues.extend(issues)
    if discipline is not None:
        output.course_content.extend(topics)
    else:
        for topic in topics:
            output.parse_issues.append(ParseIssue(
                category="manual_review",
                message=(
                    "Тема прочитана, но практика с нагрузкой не создана; "
                    f"в course_content не передана: {topic.topic}; "
                    f"{topic.activity}; {topic.hours} ч."
                ),
                source_ref=topic.source_ref,
            ))

    tools, relations, issues = extract_assessment_tools(
        document, file_name, output.indicators, doc_type="RPP",
    )
    output.assessment_tools.extend(tools)
    output.relations.extend(relations)
    output.parse_issues.extend(issues)

    scale, issues = extract_practice_score_scale(document, file_name)
    output.score_scale.extend(scale)
    output.parse_issues.extend(issues)
    return ParserOutput.model_validate(output.model_dump())


def parse_rpp(file_path):
    file_path = Path(file_path)
    if file_path.suffix.lower() != ".docx":
        raise ValueError("Ожидается файл DOCX")

    raw = file_path.read_bytes()
    document = Document(BytesIO(raw))
    title_parts = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            if heading_level(block) is not None:
                break
            title_parts.append(block.text)
        elif isinstance(block, Table):
            title_parts.extend(
                cell.text for row in block.rows for cell in row.cells
            )

    title = re.sub(r"\s+", "", "".join(title_parts)).casefold()
    if (
        "рабочаяпрограммапрактики" not in title
        or "рабочаяпрограммаучебнойдисциплины" in title
    ):
        raise ValueError("Не удалось однозначно распознать РПП по титулу")

    output = ParserOutput(meta=ParserMeta(
        doc_type="RPP",
        file_name=file_path.name,
        file_hash=sha256(raw).hexdigest(),
        parser_config="rpp_docx_v2",
    ))
    output.parse_issues.append(ParseIssue(
        category="manual_review",
        severity="info",
        message=(
            "Разбор РПП поддерживает общую нагрузку, перечни и таблицы "
            "компетенций, результаты обучения, табличное содержание, "
            "формы контроля, оценочные средства и шкалы. "
            "Диапазоны часов, текстовые этапы без часов и поля, "
            "отсутствующие в контракте, требуют проверки. "
            "Посеместровая нагрузка пока передается в manual_review."
        ),
        source_ref=SourceRef(file=file_path.name),
    ))

    discipline, issues = build_practice_discipline(
        document, file_path.name, title_parts,
    )
    output.parse_issues.extend(issues)
    if discipline is not None:
        output.disciplines.append(discipline)

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
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Выходной файл должен отличаться от исходного")

    try:
        output = parse_rpp(args.input)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    text = output.model_dump_json(indent=2)
    ParserOutput.model_validate_json(text)
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
