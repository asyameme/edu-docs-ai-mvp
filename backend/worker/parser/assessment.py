"""Разбор оценочных средств, связей с индикаторами и шкал оценивания."""

import re

from docx.text.paragraph import Paragraph

from .common import (
    normalize,
    read_section,
)
from .models import (
    AssessmentTool,
    ParseIssue,
    Relation,
    ScoreScaleItem,
)


def assessment_columns(cells):
    """Найти колонки индикаторов и оценочных средств."""
    indicator_columns = [
        index
        for index, cell in enumerate(cells)
        if "индикатор" in normalize(cell.text)
    ]
    tool_columns = [
        index
        for index, cell in enumerate(cells)
        if re.match(
            r"^(?:контрольно.?измерительные материалы|"
            r"ким\b|оценочные средства)",
            normalize(cell.text),
        )
    ]

    if not indicator_columns or len(tool_columns) != 1:
        return None

    if len({
        cells[index]._tc
        for index in indicator_columns
    }) != 1:
        return None

    if tool_columns[0] in indicator_columns:
        return None

    return {
        "indicators": indicator_columns,
        "tool": tool_columns[0],
    }


def assessment_indicator_codes(text):
    """Прочитать коды в начале строк, списке или скобках в конце."""
    code = (
        r"[А-ЯA-Z][А-ЯA-Z0-9]*"
        r"(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+"
    )
    group = rf"{code}(?:\s*[,;]\s*{code})*"
    prefix = re.compile(
        rf"^({group})(?:\.(?!\d)|:|(?=\s|$))"
    )
    suffix = re.compile(rf"\(({group})\)\.?$")

    codes = []
    uncertain = False

    for line in text.splitlines():
        line = line.strip()
        match = prefix.match(line) or suffix.search(line)

        if match:
            codes.extend(re.findall(code, match.group(1)))
        elif re.search(code, line):
            uncertain = True

    return list(dict.fromkeys(codes)), uncertain


def assessment_names(cell):
    """Разделить абзацы и явные списки, сохраняя формулировки."""
    names = []
    uncertain = False
    empty_labels = {
        "-", "\u2013", "\u2014", "нет", "не предусмотрено",
        "не предусмотрены", "отсутствует", "отсутствуют",
    }
    starts = (
        r"^(?:см\.|вопрос|контрольн|тестов|тем[аы]\b|задани|"
        r"практич|лаборатор|кейс|проект|профессиональн|инженерн|"
        r"комплект|индивидуальн|реферат|курсов|экзаменацион|"
        r"исследовательск|ситуацион|семинар)"
    )

    for paragraph in cell.paragraphs:
        lines = [
            " ".join(line.split())
            for line in paragraph.text.splitlines()
            if line.strip()
        ]
        lines = [
            line
            for line in lines
            if normalize(line) not in empty_labels
        ]

        if not lines:
            continue

        if len(lines) > 1 and not all(
            re.match(starts, normalize(line))
            for line in lines
        ):
            lines = [" ".join(lines)]
            uncertain = True

        for line in lines:
            parts = []
            buffer = []
            quote = None
            brackets = 0
            invalid = False

            for char in line:
                if quote:
                    if char == quote:
                        quote = None

                elif char in {'"', "«", "“"}:
                    quote = {
                        '"': '"',
                        "«": "»",
                        "“": "”",
                    }[char]

                elif char == "(":
                    brackets += 1

                elif char == ")":
                    brackets -= 1
                    invalid = invalid or brackets < 0

                elif char == ";" and brackets == 0:
                    parts.append("".join(buffer).strip())
                    buffer = []
                    continue

                buffer.append(char)

            if quote or brackets or invalid:
                names.append(line)
                uncertain = True
            else:
                parts.append("".join(buffer).strip())
                names.extend(
                    part
                    for part in parts
                    if part and normalize(part) not in empty_labels
                )

    return names, uncertain


def _practice_assessment_name(text):
    """Прочитать явно названное средство из короткого заголовка РПП."""
    raw = " ".join(text.split()).rstrip(".")
    if len(raw) <= 300 and "?" not in raw and normalize(raw).startswith(
        "типовые вопросы к "
    ):
        return raw
    match = re.fullmatch(
        r"(форма\s+отзыва\s+.+?)\s+в\s+приложении\s+\d+",
        raw,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def extract_assessment_tools(document, file_name, indicators, *, doc_type="RPD"):
    """Извлечь формулировки КИМ и явные связи с индикаторами."""
    if doc_type not in {"RPD", "RPP"}:
        raise ValueError("Поддерживаются РПД и РПП")

    def is_heading(paragraph):
        title = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*",
            "",
            normalize(paragraph.text),
        )

        # Нормализация используется только для поиска заголовка.
        title = title.replace("\u00ad", "").replace("-", "")

        if doc_type == "RPP":
            return title.startswith("оценочные средства:") or title in {
                "оценочные средства", "контрольноизмерительные материалы",
            }

        return (
            title.startswith(
                "методические материалы для проведения текущего контроля"
            )
            or title in {
                "оценочные средства",
                "контрольноизмерительные материалы",
            }
        )

    blocks, issues = read_section(
        document,
        file_name,
        is_heading,
        "оценочные средства",
        keep_numbered_items=True,
    )

    tools = []
    relations = []
    tools_by_name = {}
    prose_refs = {}
    known_codes = {item.code for item in indicators}
    found_table = False
    details_ref = None

    def report(category, message, ref, severity="warning"):
        issues.append(ParseIssue(
            category=category,
            message=f"Оценочные средства: {message}",
            severity=severity,
            source_ref=ref,
        ))

    for block, table_ref in blocks:
        if isinstance(block, Paragraph):
            if doc_type == "RPP":
                name = _practice_assessment_name(block.text)
                if name:
                    if name not in tools_by_name:
                        tool = AssessmentTool(name=name, source_ref=table_ref)
                        tools_by_name[name] = tool
                        tools.append(tool)
                    prose_refs.setdefault(name, table_ref)
                    continue
            details_ref = details_ref or table_ref
            continue

        header = None

        for index, row in enumerate(block.rows[:5]):
            columns = assessment_columns(row.cells)

            if columns is not None:
                header = (index, columns, len(row.cells))
                break

        if header is None:
            details_ref = details_ref or table_ref
            continue

        found_table = True
        header_index, columns, width = header
        seen_pairs = set()
        has_data = False

        for row_number, row in enumerate(
            block.rows[header_index + 1:],
            start=header_index + 2,
        ):
            cells = row.cells
            row_ref = table_ref.model_copy(
                update={"row": row_number},
            )

            if not any(cell.text.strip() for cell in cells):
                continue

            repeated = assessment_columns(cells)

            if repeated is not None:
                if repeated != columns:
                    report(
                        "unexpected_format",
                        "внутри таблицы изменились колонки; "
                        "оставшаяся часть требует проверки",
                        row_ref,
                    )
                    break

                continue

            labels = [cell.text.strip() for cell in cells]
            numbered = [label for label in labels if label]

            if (
                row_number == header_index + 2
                and sum(bool(label) for label in labels) >= 2
                and (
                    all(
                        not label or label == str(index)
                        for index, label in enumerate(labels, 1)
                    )
                    or (
                        doc_type == "RPP"
                        and numbered == [
                            str(index) for index in range(1, len(numbered) + 1)
                        ]
                    )
                )
            ):
                continue

            has_data = True

            if (
                len(cells) != width
                or row.grid_cols_before
                or row.grid_cols_after
            ):
                report(
                    "unexpected_format",
                    "строка не совпадает с шапкой",
                    row_ref,
                )
                continue

            indicator_cells = [
                cells[index]
                for index in columns["indicators"]
            ]
            tool_cell = cells[columns["tool"]]

            pair = (
                tuple(cell._tc for cell in indicator_cells)
                + (tool_cell._tc,)
            )

            if pair in seen_pairs:
                continue

            seen_pairs.add(pair)

            indicator_ref = row_ref.model_copy(update={
                "col": columns["indicators"][0] + 1,
            })
            tool_ref = row_ref.model_copy(update={
                "col": columns["tool"] + 1,
            })

            if any(
                cell._tc is tool_cell._tc
                for cell in indicator_cells
            ):
                report(
                    "ambiguous",
                    "индикатор и средство находятся в общей ячейке",
                    row_ref,
                )
                continue

            if tool_cell.tables or any(
                cell.tables for cell in indicator_cells
            ):
                report(
                    "unexpected_format",
                    "в ячейке есть вложенная таблица",
                    row_ref,
                )
                continue

            names, uncertain_names = assessment_names(tool_cell)

            has_nontext = bool(tool_cell._tc.xpath(
                ".//m:oMath|.//m:oMathPara|.//w:drawing",
            ))

            if has_nontext:
                report(
                    "manual_review",
                    "в ячейке есть формулы или изображения; "
                    "эти фрагменты не перенесены в name",
                    tool_ref,
                )

            if not names:
                report(
                    "unexpected_format" if has_nontext else "empty_cell",
                    "не удалось прочитать название оценочного средства",
                    tool_ref,
                )

            if uncertain_names:
                report(
                    "manual_review",
                    "границы названий неоднозначны; формулировка "
                    "сохранена целиком, связи для строки не созданы",
                    tool_ref,
                )

            for name in names:
                if name not in tools_by_name:
                    tool = AssessmentTool(
                        name=name,
                        source_ref=tool_ref,
                    )
                    tools_by_name[name] = tool
                    tools.append(tool)

            texts = []
            seen = set()

            for cell in indicator_cells:
                if cell._tc not in seen:
                    texts.append(cell.text)
                    seen.add(cell._tc)

            codes, uncertain_codes = assessment_indicator_codes(
                "\n".join(texts),
            )

            if not codes:
                report(
                    "empty_cell"
                    if not any(text.strip() for text in texts)
                    else "unparsed_row",
                    "не найден код индикатора",
                    indicator_ref,
                )

            if uncertain_codes:
                report(
                    "manual_review",
                    "часть записи индикатора не распознана; "
                    "связи для строки не созданы",
                    indicator_ref,
                )

            for code in codes:
                if code not in known_codes:
                    report(
                        "manual_review",
                        f"индикатор {code} не извлечен из раздела "
                        "результатов; связь с отсутствующим "
                        "объектом не создана",
                        indicator_ref,
                    )

            if uncertain_names or uncertain_codes:
                continue

            if len(codes) > 1 and len(set(names)) > 1:
                report(
                    "ambiguous",
                    "в строке несколько индикаторов и средств; "
                    "их попарное соответствие не определено",
                    row_ref,
                )
                continue

            for code in codes:
                if code in known_codes:
                    for name in names:
                        relations.append(Relation(
                            src_entity="indicator",
                            src_key=code,
                            dst_entity="assessment_tool",
                            dst_key=name,
                            kind="проверяется",
                            source_ref=row_ref,
                        ))

        if not has_data:
            report(
                "empty_cell",
                "в таблице нет содержательных строк",
                table_ref,
            )

    if blocks and not found_table and not (doc_type == "RPP" and tools):
        report(
            "manual_review",
            "не найдена поддержанная таблица индикаторов и средств; "
            "нужен отдельный разбор текста или другого формата",
            blocks[0][1],
        )

    elif details_ref is not None:
        report(
            "manual_review",
            "тексты заданий и вспомогательные таблицы пока "
            "не перенесены в JSON; у AssessmentTool есть "
            "только name и source_ref",
            details_ref,
            severity="info",
        )

    linked_names = {relation.dst_key for relation in relations}
    for name, ref in prose_refs.items():
        if name not in linked_names:
            report(
                "manual_review",
                f"название {name!r} прочитано из текста; явная связь "
                "с индикатором не найдена",
                ref,
            )

    return tools, relations, issues


def score_scale_columns(cells):
    """Сопоставить заголовки колонок полям ScoreScaleItem."""
    columns = {}

    for index, cell in enumerate(cells):
        text = normalize(cell.text).replace("\u0451", "е")
        field = None

        if "ects" in text:
            field = "grade_ects"

        elif text.startswith("оценка") and "спбгу" in text:
            field = "grade_spbu"

        elif (
            "критери" in text
            or text.startswith((
                "процент",
                "объем выполненных",
                "формулировки показателей",
                "дополнительное условие",
                "criterion",
                "criteria",
            ))
        ):
            field = "criterion"

        elif "балл" in text or "points" in text:
            field = "points"

        elif (
            re.match(r"^оценка(?:\s|$)", text)
            or text in {"результат", "exam grade", "grade"}
        ):
            field = "grade_spbu"

        if field is not None:
            if field in columns:
                return None

            columns[field] = index

    if "grade_spbu" not in columns or len(columns) < 2:
        return None

    return columns


def extract_score_scale(document, file_name):
    """Прочитать таблицы оценок, ECTS, баллов и критериев."""
    def is_heading(paragraph):
        title = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*",
            "",
            normalize(paragraph.text),
        ).replace("\u00ad", "").replace("-", "")

        return (
            title.startswith(
                "методика проведения текущего контроля"
            )
            and "критерии оценивания" in title
        ) or title.rstrip(".:") in {
            "методика и критерии оценивания",
            "шкала оценивания",
            "шкалы оценивания",
            "шкала и критерии оценивания",
        }

    # Раздел может отсутствовать в шаблоне РПД.
    present = any(
        is_heading(paragraph)
        and not paragraph.style.name.casefold().startswith((
            "toc",
            "оглавление",
        ))
        and not re.search(r"\t\s*\d+\s*$", paragraph.text)
        for paragraph in document.paragraphs
    )

    if not present:
        return [], []

    blocks, issues = read_section(
        document,
        file_name,
        is_heading,
        "шкалы и критерии оценивания",
        keep_numbered_items=True,
    )

    items = []
    found_table = False
    text_ref = None

    def report(category, message, ref, severity="warning"):
        issues.append(ParseIssue(
            category=category,
            message=f"Шкалы оценивания: {message}",
            severity=severity,
            source_ref=ref,
        ))

    for block, table_ref in blocks:
        if isinstance(block, Paragraph):
            text_ref = text_ref or table_ref
            continue

        found_table = True
        header = None

        for index, row in enumerate(block.rows[:5]):
            columns = score_scale_columns(row.cells)

            if columns is not None:
                header = (index, columns, len(row.cells))
                break

        if header is None:
            report(
                "manual_review",
                "таблица не сопоставлена полям модели; "
                "для ScoreScaleItem нужна явная колонка оценки, "
                "баллы за задание ее не заменяют",
                table_ref,
            )
            continue

        header_index, columns, width = header
        header_labels = [
            normalize(cell.text)
            for cell in block.rows[header_index].cells
        ]
        seen_cells = set()
        has_data = False

        for row_number, row in enumerate(
            block.rows[header_index + 1:],
            start=header_index + 2,
        ):
            cells = row.cells
            row_ref = table_ref.model_copy(
                update={"row": row_number},
            )

            if not any(cell.text.strip() for cell in cells):
                continue

            repeated = score_scale_columns(cells)

            if repeated is not None:
                current_labels = [
                    normalize(cell.text)
                    for cell in cells
                ]

                if (
                    repeated != columns
                    or current_labels != header_labels
                ):
                    report(
                        "unexpected_format",
                        "внутри таблицы изменилась шапка; "
                        "оставшаяся часть требует проверки",
                        row_ref,
                    )
                    break

                continue

            has_data = True
            labels = [cell.text.strip() for cell in cells]

            if (
                row_number == header_index + 2
                and sum(bool(label) for label in labels) >= 2
                and all(
                    not label or label == str(index)
                    for index, label in enumerate(labels, 1)
                )
            ):
                report(
                    "ambiguous",
                    "строка похожа на нумерацию колонок; "
                    "она не интерпретирована как оценка",
                    row_ref,
                )
                continue

            if (
                len(cells) != width
                or row.grid_cols_before
                or row.grid_cols_after
            ):
                report(
                    "unexpected_format",
                    "строка не совпадает с шапкой",
                    row_ref,
                )
                continue

            selected = {
                field: cells[index]
                for field, index in columns.items()
            }
            key = tuple(
                cell._tc
                for cell in selected.values()
            )

            if key in seen_cells:
                continue

            seen_cells.add(key)

            if len(set(key)) != len(key):
                report(
                    "ambiguous",
                    "одна ячейка объединяет разные поля шкалы",
                    row_ref,
                )
                continue

            if any(cell.tables for cell in selected.values()):
                report(
                    "unexpected_format",
                    "в ячейке есть вложенная таблица",
                    row_ref,
                )
                continue

            values = {
                field: " ".join(cell.text.split())
                for field, cell in selected.items()
            }

            grade = values["grade_spbu"]

            if re.fullmatch(
                r"(?:итого|всего)(?:\s+по\b.*|\s*[:.]?)",
                normalize(grade),
            ):
                report(
                    "manual_review",
                    f"итоговая строка {values!r} "
                    "не является строкой оценки",
                    row_ref,
                    severity="info",
                )
                continue

            if not grade or grade in {"-", "\u2013", "\u2014"}:
                has_nontext = bool(
                    selected["grade_spbu"]._tc.xpath(
                        ".//m:oMath|.//m:oMathPara|.//w:drawing",
                    )
                )

                report(
                    "unexpected_format"
                    if has_nontext
                    else "empty_cell",
                    "не удалось прочитать оценку; "
                    "строка не перенесена",
                    row_ref.model_copy(update={
                        "col": columns["grade_spbu"] + 1,
                    }),
                )
                continue

            for field in ("grade_ects", "points", "criterion"):
                value = values.get(field)
                values[field] = (
                    value
                    if value and value not in {
                        "-", "\u2013", "\u2014",
                    }
                    else None
                )

            if values["criterion"] is not None:
                label = " ".join(
                    block.rows[header_index]
                    .cells[columns["criterion"]]
                    .text.split()
                )

                if (
                    "критери" not in normalize(label)
                    and normalize(label) not in {
                        "criterion",
                        "criteria",
                    }
                ):
                    values["criterion"] = (
                        f"{label}: {values['criterion']}"
                    )

            if any(
                cell._tc.xpath(
                    ".//m:oMath|.//m:oMathPara|.//w:drawing"
                )
                for cell in selected.values()
            ):
                report(
                    "manual_review",
                    "формулы или изображения не перенесены; "
                    "сохранены доступные текстовые значения",
                    row_ref,
                )

            items.append(ScoreScaleItem(
                grade_spbu=grade,
                grade_ects=values["grade_ects"],
                points=values["points"],
                criterion=values["criterion"],
                source_ref=row_ref.model_copy(update={
                    "col": columns["grade_spbu"] + 1,
                }),
            ))

        if not has_data:
            report(
                "empty_cell",
                "в таблице нет строк оценок",
                table_ref,
            )

    if blocks and not found_table:
        report(
            "manual_review",
            "раздел содержит текст без поддержанной таблицы; "
            "шкала из текста пока не извлечена",
            blocks[0][1],
        )

    elif text_ref is not None:
        report(
            "manual_review",
            "текстовые правила вне таблиц пока не перенесены "
            "в score_scale",
            text_ref,
            severity="info",
        )

    return items, issues


def extract_practice_score_scale(document, file_name):
    """Прочитать шкалы РПП, включая параллельные колонки оценок."""
    items, issues = extract_score_scale(document, file_name)

    def is_heading(paragraph):
        title = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*", "", normalize(paragraph.text),
        ).rstrip(".:")
        return title == "методика и критерии оценивания"

    blocks, _ = read_section(
        document, file_name, is_heading, "шкалы практики",
        keep_numbered_items=True,
    )
    # Текстовая бинарная шкала: сохраняем весь набор требований,
    # включая слова "всех" и "одного", определяющие условия оценки.
    if blocks and all(isinstance(block, Paragraph) for block, _ in blocks):
        paragraphs = [" ".join(block.text.split()) for block, _ in blocks]
        grade_mentions = re.compile(
            r'(?:для\s+оценки|выставляется\s+оценка)\s*[«"]'
            r'((?:не\s+)?зачтено)[»"]',
            re.IGNORECASE,
        )
        if len(paragraphs) > 1 and re.match(
            r"для\s+оценки\b", normalize(paragraphs[0]),
        ):
            seen_grades = set()
            criterion = "\n".join(paragraphs)
            for text, (_, ref) in zip(paragraphs, blocks):
                for match in grade_mentions.finditer(text):
                    grade = match.group(1)
                    key = normalize(grade)
                    if key in seen_grades:
                        continue
                    seen_grades.add(key)
                    items.append(ScoreScaleItem(
                        grade_spbu=grade,
                        criterion=criterion,
                        source_ref=ref,
                    ))
            if seen_grades:
                issues = [
                    issue for issue in issues
                    if not issue.message.startswith(
                        "Шкалы оценивания: раздел содержит текст без поддержанной таблицы"
                    )
                ]
    resolved_tables = set()
    existing_tables = {item.source_ref.table for item in items}
    grade_pattern = re.compile(
        r"((?:не\s+)?зачтено|неудовлетворительно|удовлетворительно|"
        r"хорошо|отлично)(?:\s*\(([A-F])\))?",
        re.IGNORECASE,
    )

    for block, table_ref in blocks:
        if isinstance(block, Paragraph) or table_ref.table in existing_tables:
            continue
        header = None
        for index, row in enumerate(block.rows[:5]):
            labels = [normalize(cell.text) for cell in row.cells]
            grades = [i for i, text in enumerate(labels) if text == "оценка"]
            criteria = [i for i, text in enumerate(labels) if text == "критерии"]
            if len(grades) == 2 and len(criteria) == 1 and len(labels) == 3:
                header = index, grades, criteria[0], len(labels)
                break
        if header is None:
            continue

        index, grades, criterion_column, width = header
        before_count = len(items)
        seen = set()
        for row_number, row in enumerate(block.rows[index + 1:], index + 2):
            cells = row.cells
            ref = table_ref.model_copy(update={"row": row_number})
            if not any(cell.text.strip() for cell in cells):
                continue
            if (
                len(cells) != width
                or row.grid_cols_before
                or row.grid_cols_after
            ):
                issues.append(ParseIssue(
                    category="unexpected_format",
                    message="Шкала практики: строка не совпадает с шапкой",
                    source_ref=ref,
                ))
                continue
            labels = [normalize(cell.text) for cell in cells]
            if labels[grades[0]] == labels[grades[1]] == "оценка":
                continue
            criterion_cell = cells[criterion_column]
            criterion = " ".join(criterion_cell.text.split()) or None
            if criterion is None:
                issues.append(ParseIssue(
                    category="empty_cell",
                    message="Шкала практики: не указан критерий оценки",
                    source_ref=ref.model_copy(update={"col": criterion_column + 1}),
                ))

            for column in grades:
                grade_cell = cells[column]
                key = grade_cell._tc, criterion_cell._tc
                if key in seen:
                    continue
                seen.add(key)
                grade = " ".join(grade_cell.text.split())
                grade_ref = ref.model_copy(update={"col": column + 1})
                if grade_cell._tc is criterion_cell._tc:
                    issues.append(ParseIssue(
                        category="ambiguous",
                        message="Шкала практики: оценка и критерий объединены",
                        source_ref=grade_ref,
                    ))
                    continue
                if grade_cell.tables or criterion_cell.tables:
                    issues.append(ParseIssue(
                        category="unexpected_format",
                        message="Шкала практики: в ячейке есть вложенная таблица",
                        source_ref=grade_ref,
                    ))
                    continue
                match = grade_pattern.fullmatch(grade)
                if match is None:
                    issues.append(ParseIssue(
                        category="unparsed_row",
                        message=f"Шкала практики: не распознана оценка {grade!r}",
                        source_ref=grade_ref,
                    ))
                    continue
                items.append(ScoreScaleItem(
                    grade_spbu=grade,
                    grade_ects=match.group(2).upper() if match.group(2) else None,
                    criterion=criterion,
                    source_ref=grade_ref,
                ))

        if len(items) > before_count:
            resolved_tables.add(table_ref.table)

    issues = [
        issue for issue in issues
        if not (
            issue.source_ref is not None
            and issue.source_ref.table in resolved_tables
            and issue.message.startswith(
                "Шкалы оценивания: таблица не сопоставлена полям модели"
            )
        )
    ]
    return items, issues
