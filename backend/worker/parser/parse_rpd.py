"""Сборка результата разбора РПД и запуск из командной строки.

Функции из отдельных модулей также доступны по прежним импортам
из parse_rpd для совместимости с существующими проверками."""

import argparse
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from .assessment import (
    assessment_columns,
    assessment_indicator_codes,
    assessment_names,
    extract_assessment_tools,
    extract_score_scale,
    score_scale_columns,
)
from .common import (
    heading_level,
    normalize,
    parse_workload_number,
    read_section,
)
from .content import (
    course_content_columns,
    extract_course_content,
    extract_wide_course_table,
    wide_course_header,
)
from .discipline import (
    build_discipline,
    extract_control_forms,
    extract_discipline_name,
    extract_prerequisites,
    extract_workload,
    organization_tables,
    organization_variant_rows,
    select_workload_variant,
)
from .models import (
    ParseIssue,
    ParserMeta,
    ParserOutput,
    SourceRef,
)
from .results import (
    extract_indicators,
    extract_learning_outcomes,
    extract_results_into,
    is_results_heading,
    split_competencies,
    split_competency,
)

__all__ = [
    'normalize',
    'heading_level',
    'is_results_heading',
    'split_competency',
    'split_competencies',
    'organization_tables',
    'extract_control_forms',
    'organization_variant_rows',
    'select_workload_variant',
    'build_discipline',
    'extract_indicators',
    'extract_learning_outcomes',
    'parse_workload_number',
    'extract_workload',
    'extract_discipline_name',
    'read_section',
    'extract_prerequisites',
    'course_content_columns',
    'wide_course_header',
    'extract_wide_course_table',
    'extract_course_content',
    'assessment_columns',
    'assessment_indicator_codes',
    'assessment_names',
    'extract_assessment_tools',
    'score_scale_columns',
    'extract_score_scale',
    'finalize_rpd',
    'parse_rpd',
    'extract_results_into',
    'main',
]


def finalize_rpd(output, document, file_name):
    """Собрать независимые разделы и проверить общий результат."""
    tools, relations, issues = extract_assessment_tools(
        document,
        file_name,
        output.indicators,
    )
    output.assessment_tools.extend(tools)
    output.relations.extend(relations)
    output.parse_issues.extend(issues)

    scale, scale_issues = extract_score_scale(
        document,
        file_name,
    )
    output.score_scale.extend(scale)
    output.parse_issues.extend(scale_issues)

    return ParserOutput.model_validate(output.model_dump())


def parse_rpd(
    file_path,
    workload_track=None,
    education_mode=None,
):
    file_path = Path(file_path)

    if file_path.suffix.lower() != ".docx":
        raise ValueError("Ожидается файл DOCX")

    raw = file_path.read_bytes()
    document = Document(BytesIO(raw))
    blocks = list(document.iter_inner_content())

    # 1. Читаем титул до первого заголовка.
    title_parts = []

    for block in blocks:
        if isinstance(block, Paragraph):
            if heading_level(block) is not None:
                break

            title_parts.append(block.text)

        elif isinstance(block, Table):
            title_parts.extend(
                cell.text
                for row in block.rows
                for cell in row.cells
            )

    title = re.sub(
        r"\s+",
        "",
        "".join(title_parts),
    ).casefold()

    is_rpd = "рабочаяпрограммаучебнойдисциплины" in title
    is_rpp = "рабочаяпрограммапрактики" in title

    if not is_rpd or is_rpp:
        raise ValueError(
            "Эта версия поддерживает только "
            "однозначно распознанную РПД"
        )

    # 2. Создаем результат и метаданные файла.
    output = ParserOutput(
        meta=ParserMeta(
            doc_type="RPD",
            file_name=file_path.name,
            file_hash=sha256(raw).hexdigest(),
            parser_config="rpd_score_scale_v1",
        )
    )

    output.parse_issues.append(
        ParseIssue(
            category="manual_review",
            severity="info",
            message=(
                "Частичный разбор: извлекаются дисциплина, нагрузка, "
                "компетенции, индикаторы, результаты обучения, "
                "пререквизиты, табличное содержание, оценочные средства "
                "и табличные шкалы оценивания. "
            ),
            source_ref=SourceRef(file=file_path.name),
        )
    )

    # 3. Ищем раздел по заголовку, независимо от номера.
    discipline, discipline_issues = build_discipline(
        document,
        file_path.name,
        title_parts,
        workload_track=workload_track,
        education_mode=education_mode,
    )
    output.parse_issues.extend(discipline_issues)

    if discipline is not None:
        output.disciplines.append(discipline)
    prerequisite_records, prerequisite_issues = extract_prerequisites(
        document,
        file_path.name,
    )
    output.parse_issues.extend(prerequisite_issues)

    if output.disciplines:
        discipline = output.disciplines[0]

        discipline.prerequisites = [
            record["name"]
            for record in prerequisite_records
        ]

        sections = [
            part.strip()
            for part in (
                discipline.source_ref.section or ""
            ).split(";")
            if part.strip()
        ]

        for record in prerequisite_records:
            section = record["source_ref"].section

            if section and section not in sections:
                sections.append(section)

        discipline.source_ref.section = "; ".join(
            section
            for section in sections
            if section
        )

    elif prerequisite_records:
        output.parse_issues.append(ParseIssue(
            category="manual_review",
            message=(
                "Пререквизиты найдены, но Discipline не создан; "
                "перечень не передан в JSON"
            ),
            source_ref=prerequisite_records[0]["source_ref"],
        ))

    selected_semesters = (
        discipline.semesters
        if discipline is not None
        else None
    )

    course_topics, content_issues = extract_course_content(
        document,
        file_path.name,
        semesters=selected_semesters,
    )

    output.parse_issues.extend(content_issues)

    if output.disciplines:
        output.course_content.extend(course_topics)

    elif course_topics:
        output.parse_issues.append(ParseIssue(
            category="manual_review",
            message=(
                "Содержание прочитано, но Discipline не создан; "
                "темы не переданы в JSON"
            ),
            source_ref=course_topics[0].source_ref,
        ))

    output = extract_results_into(
        output,
        document,
        file_path.name,
        is_results_heading,
    )
    return finalize_rpd(output, document, file_path.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
    )
    parser.add_argument("--workload-track")
    parser.add_argument(
        "--education-mode",
        choices=["очная", "заочная", "очно-заочная"],
    )
    args = parser.parse_args()

    if args.input.resolve() == args.output.resolve():
        parser.error(
            "Выходной файл должен отличаться от исходного"
        )

    try:
        result = parse_rpd(
            args.input,
            workload_track=args.workload_track,
            education_mode=args.education_mode,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))

    json_text = result.model_dump_json(indent=2)

    #проверяем, что сохраненный JSON читается по той же схеме
    ParserOutput.model_validate_json(json_text)

    args.output.write_text(json_text, encoding="utf-8")

    print(
        f"Дисциплин: {len(result.disciplines)}. "
        f"Компетенций: {len(result.competencies)}. "
        f"Индикаторов: {len(result.indicators)}. "
        f"Результатов обучения: {len(result.learning_outcomes)}. "
    )


if __name__ == "__main__":
    main()
