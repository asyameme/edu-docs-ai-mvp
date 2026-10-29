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
    text = re.sub(r"\bперечен\s+ь\b", "перечень", text)
    return text in {
        "перечень результатов обучения",
        "перечень результатов обучения (learning outcomes)",
        "перечень результатов обучения (learningoutcomes)",
    }


def split_competency(text):
    """Выделить код и описание компетенции."""
    text = " ".join(text.split())
    code_pattern = r"[А-ЯA-Z][А-ЯA-Z0-9]*(?:-[А-ЯA-Z0-9]+)+"

    match = re.fullmatch(
        rf"({code_pattern})[.,:]?(?:\s+(.*))?",
        text,
    )

    if match:
        return match.group(1), match.group(2) or ""

    match = re.fullmatch(
        rf"(.+?)\s*\(({code_pattern})\)\.?",
        text,
    )

    if match:
        return match.group(2), match.group(1).strip()

    return None

def split_competencies(text):
    """Разделить несколько записей компетенций в одной ячейке."""
    parts = []
    current = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        if current and split_competency(line) is not None:
            parts.append(" ".join(current))
            current = []

        current.append(line)

    if current:
        parts.append(" ".join(current))

    return [split_competency(part) for part in parts]

def extract_indicators(text, competency_code, source_ref):
    """Разобрать индикаторы одной ячейки."""
    indicators = []
    issues = []
    pattern = re.compile(
        r"(?<!\S)"
        r"([А-ЯA-Z][А-ЯA-Z0-9]*(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+)"
        r"(?:\.(?!\d)|:|(?=\s|$))"
    )
    suffix_pattern = re.compile(
        r"(.+?)\s*\("
        r"([А-ЯA-Z][А-ЯA-Z0-9]*(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+)"
        r"\)\.?"
    )
    indicator_code = (
        r"[А-ЯA-Z][А-ЯA-Z0-9]*"
        r"(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+"
    )
    group_pattern = re.compile(
        rf"({indicator_code}(?:\s*;\s*{indicator_code})+)"
        r"\s*:\s*(.+)"
    )
    lines = []

    for line in text.splitlines():
        group = group_pattern.fullmatch(line.strip())

        if group:
            codes = re.split(r"\s*;\s*", group.group(1))
            description = group.group(2)

            lines.extend(
                f"{code} {description}"
                for code in codes
            )
            continue

        suffix = suffix_pattern.fullmatch(line.strip())

        if suffix and not pattern.match(line.strip()):
            line = f"{suffix.group(2)} {suffix.group(1).strip()}"

        lines.append(line)

    text = "\n".join(lines)
    matches = list(pattern.finditer(text))

    if not matches:
        repeated_competency = split_competency(text)

        if repeated_competency:
            issues.append(ParseIssue(
                category="manual_review",
                message=(
                    "В колонке индикаторов указан код компетенции "
                    f"{repeated_competency[0]}, а не код индикатора"
                ),
                source_ref=source_ref,
            ))
        else:
            issues.append(ParseIssue(
                category=(
                    "empty_cell"
                    if not text.strip()
                    else "unparsed_row"
                ),
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
            parser_config="rpd_competencies_indicators_v2",
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
        header_row = None
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
                header_row = row_number
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
            labels = [cell.text.strip() for cell in row.cells]

            if (
                row_number == header_row + 1
                and sum(bool(label) for label in labels) >= 2
                and all(
                    not label or label == str(index)
                    for index, label in enumerate(labels, 1)
                )
            ):
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
            parsed_items = split_competencies(cell.text)
            row_codes = []
            new_cell = cell._tc not in seen_cells

            if not parsed_items:
                output.parse_issues.append(ParseIssue(
                    category="manual_review",
                    message=(
                        "В строке есть данные, но не указана компетенция; "
                        "нужно проверить принадлежность этих данных"
                    ),
                    source_ref=ref,
                ))
                continue

            for parsed in parsed_items:
                if parsed is None:
                    output.parse_issues.append(ParseIssue(
                        category="unparsed_row",
                        message="Не удалось разобрать запись компетенции",
                        source_ref=ref,
                    ))
                    continue

                competency_code, formulation = parsed

                if not formulation:
                    known_formulations = {
                        competency.formulation
                        for competency in output.competencies
                        if competency.code == competency_code
                        and competency.source_ref.table == table_number
                    }

                    if len(known_formulations) != 1:
                        output.parse_issues.append(ParseIssue(
                            category=(
                                "manual_review"
                                if not known_formulations
                                else "ambiguous"
                            ),
                            message=(
                                f"Для кода {competency_code} выше в таблице "
                                "не найдено полного описания"
                                if not known_formulations
                                else f"У кода {competency_code} выше в таблице "
                                "есть разные описания"
                            ),
                            source_ref=ref,
                        ))
                        continue

                if formulation and new_cell:
                    output.competencies.append(Competency(
                        code=competency_code,
                        formulation=formulation,
                        source_ref=ref,
                    ))

                row_codes.append(competency_code)

            seen_cells.add(cell._tc)
            row_codes = list(dict.fromkeys(row_codes))

            if not row_codes or indicator_column is None:
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
                row_codes[0],
                indicator_ref,
            )
            output.parse_issues.extend(issues)

            for indicator in indicators:
                if len(parsed_items) > 1:
                    parents = [
                        code for code in row_codes
                        if indicator.code.startswith(code + ".")
                    ]

                    if len(parents) != 1:
                        output.parse_issues.append(ParseIssue(
                            category="ambiguous",
                            message=(
                                "Не удалось определить компетенцию "
                                f"индикатора {indicator.code} в этой строке"
                            ),
                            source_ref=indicator_ref,
                        ))
                        continue

                    indicator = Indicator(
                        code=indicator.code,
                        text=indicator.text,
                        competency_code=parents[0],
                        source_ref=indicator_ref,
                    )

                output.indicators.append(indicator)
                output.relations.append(Relation(
                    src_entity="competency",
                    src_key=indicator.competency_code,
                    dst_entity="indicator",
                    dst_key=indicator.code,
                    kind="имеет",
                    source_ref=indicator_ref,
                ))
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