import argparse
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from .models import (
    Competency,
    Indicator,
    Relation,
    ParseIssue,
    ParserMeta,
    ParserOutput,
    SourceRef,
)


def normalize(text):
    """Подготовить текст для поиска."""
    return " ".join(text.casefold().split())


def heading_level(paragraph):
    """Определить уровень заголовка по номеру."""
    text = paragraph.text.strip()

    if re.match(
        r"^раздел\s+\d+(?:[.\s]|$)",
        normalize(text),
    ):
        return 1

    match = re.match(
        r"^(\d+(?:\.\d+)*)(?:\.\s*|\s+)([^\d\s].*)$",
        text,
    )

    if match:
        return len(match.group(1).split("."))

    return None


def is_results_heading(paragraph):
    """Найти заголовок результатов обучения по тексту."""
    if paragraph.style.name.casefold().startswith("toc"):
        return False

    # Пропускаем строки оглавления с номером страницы.
    if re.search(r"\t\s*\d+\s*$", paragraph.text):
        return False

    text = re.sub(
        r"^\d+(?:\.\d+)*\.?\s*",
        "",
        normalize(paragraph.text),
    )

    return text in {
        "перечень результатов обучения",
        "перечень результатов обучения (learning outcomes)",
        "перечень результатов обучения (learningoutcomes)",
    }

def extract_indicators(text, competency_code, source_ref):
    """Разобрать индикаторы одной ячейки."""
    indicators = []
    issues = []
    pattern = re.compile(
        r"(?<!\S)"
        r"([А-ЯA-Z][А-ЯA-Z0-9]*(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+)"
        r"(?:\.(?!\d)|(?=\s|$))"
    )
    matches = list(pattern.finditer(text))

    if not matches:
        issues.append(ParseIssue(
            category="empty_cell" if not text.strip() else "unparsed_row",
            message="В ячейке не найден код индикатора",
            source_ref=source_ref,
        ))
        return indicators, issues

    if text[:matches[0].start()].strip():
        issues.append(ParseIssue(
            category="unparsed_row",
            message="Есть неразобранный текст перед первым индикатором",
            source_ref=source_ref,
        ))

    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )
        description = " ".join(text[match.end():end].split())

        if not description:
            issues.append(ParseIssue(
                category="empty_cell",
                message=f"Нет описания индикатора {match.group(1)}",
                source_ref=source_ref,
            ))
            continue

        indicators.append(Indicator(
            code=match.group(1),
            text=description,
            competency_code=competency_code,
            source_ref=source_ref,
        ))

    return indicators, issues

def parse_rpd(file_path):
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
            parser_config="rpd_competencies_indicators_v1",
        )
    )

    output.parse_issues.append(
        ParseIssue(
            category="manual_review",
            severity="info",
            message=(
                "Частичный разбор: "
                "извлекаются только компетенции и индикаторы."
            ),
            source_ref=SourceRef(file=file_path.name),
        )
    )

    # 3. Ищем раздел по заголовку, независимо от номера.
    starts = [
        index
        for index, block in enumerate(blocks)
        if isinstance(block, Paragraph)
        and is_results_heading(block)
    ]

    if len(starts) != 1:
        output.parse_issues.append(
            ParseIssue(
                category="not_found" if not starts else "ambiguous",
                message=(
                    "Не найден раздел результатов обучения"
                    if not starts
                    else "Найдено несколько разделов результатов обучения"
                ),
                source_ref=SourceRef(file=file_path.name),
            )
        )
        return output

    start = starts[0]
    section = blocks[start].text.strip()
    depth = heading_level(blocks[start]) or 2

    #считаем таблицы от начала документа
    table_number = sum(
        isinstance(block, Table)
        for block in blocks[:start]
    )

    found_table = False

    #читаем содержимое найденного раздела
    for block in blocks[start + 1:]:
        if isinstance(block, Paragraph):
            level = heading_level(block)

            if level is not None and level <= depth:
                break

            continue

        if not isinstance(block, Table):
            continue

        table_number += 1
        column = None
        indicator_column = None
        seen_cells = set()
        seen_indicator_cells = set()

        for row_number, row in enumerate(block.rows, 1):
            headers = [
                normalize(cell.text)
                for cell in row.cells
            ]

            if "код и наименование компетенции" in headers:
                column = headers.index(
                    "код и наименование компетенции"
                )
                found_table = True

                indicator_columns = [
                    index
                    for index, header in enumerate(headers)
                    if any(
                        phrase in header
                        for phrase in (
                            "код индикатора",
                            "код и наименование индикатора",
                        )
                    )
                ]

                if len(indicator_columns) == 1:
                    indicator_column = indicator_columns[0]
                else:
                    indicator_column = None

                    output.parse_issues.append(ParseIssue(
                        category=(
                            "not_found"
                            if not indicator_columns
                            else "ambiguous"
                        ),
                        message=(
                            "Не удалось однозначно найти "
                            "колонку индикаторов"
                        ),
                        source_ref=SourceRef(
                            file=file_path.name,
                            section=section,
                            table=table_number,
                            row=row_number,
                        ),
                    ))

                continue

            if column is None:
                continue

            ref = SourceRef(
                file=file_path.name,
                section=section,
                table=table_number,
                row=row_number,
                col=column + 1,
            )

            if column >= len(row.cells):
                output.parse_issues.append(
                    ParseIssue(
                        category="unexpected_format",
                        message=(
                            "В строке отсутствует "
                            "колонка компетенции"
                        ),
                        source_ref=ref,
                    )
                )
                continue

            cell = row.cells[column]

            text = " ".join(cell.text.split())

            if not any(
                cell.text.strip()
                for cell in row.cells
            ):
                continue

            # Примеры: УК-1, ОПК-1, ПКП-УНК-03
            match = re.fullmatch(
                r"([А-ЯA-Z][А-ЯA-Z0-9]*"
                r"(?:-[А-ЯA-Z0-9]+)+)"
                r"\.?(?:\s+(.*))?",
                text,
            )

            if match and match.group(2):
                competency_code = match.group(1)

                if cell._tc not in seen_cells:
                    output.competencies.append(Competency(
                        code=competency_code,
                        formulation=match.group(2),
                        source_ref=ref,
                    ))
                    seen_cells.add(cell._tc)

                if indicator_column is None:
                    continue

                indicator_ref = SourceRef(
                    file=file_path.name,
                    section=section,
                    table=table_number,
                    row=row_number,
                    col=indicator_column + 1,
                )

                if indicator_column >= len(row.cells):
                    output.parse_issues.append(ParseIssue(
                        category="unexpected_format",
                        message="В строке отсутствует ячейка индикаторов",
                        source_ref=indicator_ref,
                    ))
                    continue

                indicator_cell = row.cells[indicator_column]

                if indicator_cell._tc in seen_indicator_cells:
                    continue

                seen_indicator_cells.add(indicator_cell._tc)

                indicators, issues = extract_indicators(
                    indicator_cell.text,
                    competency_code,
                    indicator_ref,
                )

                output.indicators.extend(indicators)
                output.parse_issues.extend(issues)

                for indicator in indicators:
                    output.relations.append(Relation(
                        src_entity="competency",
                        src_key=competency_code,
                        dst_entity="indicator",
                        dst_key=indicator.code,
                        kind="имеет",
                        source_ref=indicator_ref,
                    ))
            else:
                category = (
                    "empty_cell"
                    if not text or match
                    else "unparsed_row"
                )

                output.parse_issues.append(
                    ParseIssue(
                        category=category,
                        message=(
                            "Не удалось получить код "
                            "и формулировку компетенции"
                        ),
                        source_ref=ref,
                    )
                )

    if not found_table or not output.competencies:
        output.parse_issues.append(
            ParseIssue(
                category=(
                    "not_found"
                    if not found_table
                    else "empty_cell"
                ),
                message=(
                    "Не найдена таблица компетенций"
                    if not found_table
                    else "Из таблицы не извлечено ни одной компетенции"
                ),
                source_ref=SourceRef(
                    file=file_path.name,
                    section=section,
                ),
            )
        )

    return ParserOutput.model_validate(output.model_dump())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    if args.input.resolve() == args.output.resolve():
        parser.error(
            "Выходной файл должен отличаться от исходного"
        )

    try:
        result = parse_rpd(args.input)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    json_text = result.model_dump_json(indent=2)

    #проверяем, что сохраненный JSON читается по той же схеме
    ParserOutput.model_validate_json(json_text)

    args.output.write_text(json_text, encoding="utf-8")

    print(
        f"Компетенций: {len(result.competencies)}. "
        f"Индикаторов: {len(result.indicators)}. "
    )


if __name__ == "__main__":
    main()