"""Разбор тем, видов занятий и часов из таблиц содержания."""

import re

from docx.text.paragraph import Paragraph

from .common import (
    normalize,
    parse_workload_number,
    read_section,
)
from backend.contracts.parser_output import (
    CourseTopic,
    ParseIssue,
)


def course_content_columns(cells):
    """Найти колонки темы, вида занятий и часов по шапке."""
    patterns = {
        "topic": r"^(?:(?:наименование|название)\s+темы\b.*|тема)$",
        "activity": r"^вид(?:ы)?\s+(?:учебных\s+)?занятий$",
        "hours": r"^(?:количество\s+часов|часы)$",
    }
    columns = {}

    for field, pattern in patterns.items():
        matches = [
            index
            for index, cell in enumerate(cells)
            if re.fullmatch(pattern, normalize(cell.text))
        ]

        if len(matches) != 1:
            return None

        columns[field] = matches[0]

    return columns


def wide_course_header(rows):
    """Разобрать шапку с отдельными колонками видов занятий."""
    paths = []
    activities_pattern = (
        r"^(?:лекци|практич|семинар|лаборатор|"
        r"самостоятельн|контрольн|коллоквиум|консультаци)"
    )

    for index, row in enumerate(rows[:5]):
        cells = row.cells

        if not paths:
            paths = [[] for _ in cells]

        if (
            len(cells) != len(paths)
            or row.grid_cols_before
            or row.grid_cols_after
        ):
            return None

        # Числовые значения означают начало строк данных.
        if any(
            re.fullmatch(r"\d+(?:[.,]\d+)?", cell.text.strip())
            for cell in cells
        ):
            break

        for column, cell in enumerate(cells):
            label = " ".join(cell.text.split())

            if label and label not in paths[column]:
                paths[column].append(label)

        topic_columns = [
            column
            for column, labels in enumerate(paths)
            if any(
                normalize(label) in {"тема", "темы"}
                or re.match(
                    r"^(?:наименование|название)\s+"
                    r"(?:тем|раздел|модул)",
                    normalize(label),
                )
                for label in labels
            )
        ]

        if len(topic_columns) != 1:
            continue

        activities = {}
        references = {}
        unknown = {}
        ready = True

        for column, labels in enumerate(paths):
            if column == topic_columns[0]:
                continue

            text = normalize(" ".join(labels))
            has_hours = re.search(
                r"\bчас(?:ы|ов|а)?\b|\bч\.(?:\s|$)",
                text,
            )

            activity_labels = [
                label
                for label in labels
                if re.match(
                    activities_pattern,
                    normalize(label),
                )
            ]

            # "Всего" под видом занятий остается итогом.
            reference_labels = [
                label
                for label in labels
                if re.match(
                    r"^(?:всего|итого|контактное время)\b",
                    normalize(label),
                )
            ]

            last_is_reference = (
                reference_labels
                and (
                    not activity_labels
                    or labels.index(reference_labels[-1])
                    > labels.index(activity_labels[-1])
                )
            )

            if last_is_reference and has_hours:
                references[column] = " / ".join(labels)

            elif activity_labels and not has_hours:
                ready = False

            elif activity_labels:
                activities[column] = activity_labels[-1]

            elif has_hours:
                if re.search(
                    r"\b(?:всего|итого|контактное время|"
                    r"аудиторные занятия|контактная работа)\b",
                    text,
                ):
                    references[column] = " / ".join(labels)
                else:
                    unknown[column] = " / ".join(labels)

        if activities and ready:
            return {
                "end": index,
                "topic": topic_columns[0],
                "activities": activities,
                "references": references,
                "unknown": unknown,
                "width": len(paths),
            }

    return None


def extract_wide_course_table(table, source_ref):
    """Преобразовать строку темы в записи по видам занятий."""
    rows = table.rows
    header = wide_course_header(rows)

    if header is None:
        return None

    topics = []
    issues = []
    topic_column = header["topic"]
    activities = header["activities"]

    def report(
        category,
        message,
        row=None,
        column=None,
        severity="warning",
    ):
        issues.append(ParseIssue(
            category=category,
            message=f"Содержание: {message}",
            severity=severity,
            source_ref=source_ref.model_copy(update={
                "row": row,
                "col": None if column is None else column + 1,
            }),
        ))

    if len(set(map(normalize, activities.values()))) != len(activities):
        report(
            "ambiguous",
            "одинаковые виды занятий встречаются в нескольких "
            "колонках; нужно уточнить их назначение",
            header["end"] + 1,
        )
        return topics, issues

    for column, label in header["unknown"].items():
        report(
            "unexpected_format",
            f"неподдержанная колонка часов {label!r}; "
            "ее значения не перенесены",
            header["end"] + 1,
            column,
        )

    owners = {}

    for row in rows[header["end"] + 1:]:
        if len(row.cells) != header["width"]:
            continue

        cells = row.cells

        for column in activities:
            owners.setdefault(cells[column]._tc, set()).add(
                (cells[topic_column]._tc, column)
            )

    seen = set()
    index = header["end"] + 1

    while index < len(rows):
        row = rows[index]
        row_number = index + 1
        cells = row.cells

        if not any(cell.text.strip() for cell in cells):
            index += 1
            continue

        repeated = wide_course_header(rows[index:index + 5])

        if repeated is not None:
            if any(
                repeated[key] != header[key]
                for key in (
                    "topic",
                    "activities",
                    "references",
                    "unknown",
                    "width",
                )
            ):
                report(
                    "unexpected_format",
                    "внутри таблицы изменилась шапка; "
                    "оставшаяся часть требует проверки",
                    row_number,
                )
                break

            index += repeated["end"] + 1
            continue

        index += 1

        if (
            len(cells) != header["width"]
            or row.grid_cols_before
            or row.grid_cols_after
        ):
            report(
                "unexpected_format",
                "строка не совпадает с сеткой шапки",
                row_number,
            )
            continue

        topic_cell = cells[topic_column]
        topic = " ".join(topic_cell.text.split())

        if re.fullmatch(
            r"(?:итого|всего)(?:\s+по\b.*|\s*[:.]?)",
            normalize(topic),
        ):
            continue

        if not topic:
            report(
                "empty_cell",
                "не указана тема",
                row_number,
                topic_column,
            )
            continue

        for column, label in header["references"].items():
            raw = " ".join(cells[column].text.split())

            if raw:
                report(
                    "manual_review",
                    f"справочное значение {label!r} = {raw!r} "
                    "сохранено в сообщении; отдельная тема не создана",
                    row_number,
                    column,
                    severity="info",
                )

        for column, activity in activities.items():
            cell = cells[column]
            key = (topic_cell._tc, cell._tc, column)

            if key in seen:
                continue

            seen.add(key)

            if (
                len(owners[cell._tc]) > 1
                or sum(
                    other._tc is cell._tc
                    for other in cells
                ) > 1
            ):
                report(
                    "ambiguous",
                    "объединенные часы относятся к нескольким "
                    "темам или колонкам; распределение не определено",
                    row_number,
                    column,
                )
                continue

            if topic_cell.tables or cell.tables:
                report(
                    "unexpected_format",
                    "в ячейке обнаружена вложенная таблица",
                    row_number,
                    column,
                )
                continue

            raw = " ".join(cell.text.split())

            if not raw:
                report(
                    "empty_cell",
                    f"нет часов: тема {topic!r}, вид {activity!r}",
                    row_number,
                    column,
                )
                continue

            try:
                hours = parse_workload_number(raw)
            except ValueError:
                hours = None

            if hours is None or hours != hours.to_integral_value():
                report(
                    "unexpected_format",
                    f"часы {raw!r} нельзя записать целым числом: "
                    f"тема {topic!r}, вид {activity!r}",
                    row_number,
                    column,
                )
                continue

            topics.append(CourseTopic(
                topic=topic,
                activity=activity,
                hours=int(hours),
                source_ref=source_ref.model_copy(update={
                    "row": row_number,
                    "col": column + 1,
                }),
            ))

    if not topics and not issues:
        report(
            "empty_cell",
            "в таблице нет строк тем с часами",
            header["end"] + 1,
        )

    return topics, issues


def extract_course_content(document, file_name, semesters=None, *, doc_type="RPD"):
    """Прочитать таблицы: тема - вид занятий - часы."""
    if doc_type not in {"RPD", "RPP"}:
        raise ValueError("Поддерживаются РПД и РПП")

    def is_heading(paragraph):
        title = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*",
            "",
            normalize(paragraph.text),
        ).rstrip(".:")

        if doc_type == "RPP":
            return title == "структура и содержание практики"

        return title in {
            "структура и содержание учебных занятий",
            "структура и содержание дисциплины",
        }

    blocks, issues = read_section(
        document,
        file_name,
        is_heading,
        "содержание практики" if doc_type == "RPP" else "содержание дисциплины",
        keep_numbered_items=True,
    )

    for issue in issues:
        if issue.category == "empty_cell":
            issue.category = "manual_review"
            issue.message = (
                "Содержание: не удалось выделить содержимое раздела; "
                "нужно проверить его границы и формат"
            )

    topics = []
    table_found = False
    text_ref = None
    content_semester = None
    period_caption = None

    def report(category, message, ref, severity="warning"):
        issues.append(ParseIssue(
            category=category,
            message=f"Содержание: {message}",
            severity=severity,
            source_ref=ref,
        ))

    for block, table_ref in blocks:
        if isinstance(block, Paragraph):
            text = normalize(block.text)

            if text.startswith("период обучения"):
                period_caption = " ".join(block.text.split())

                match = re.fullmatch(
                    r"период обучения\s*(?:\(модуль\))?"
                    r"\s*:\s*семестр\s+([1-8])",
                    text,
                )
                content_semester = (
                    int(match.group(1))
                    if match
                    else None
                )

            if not text.startswith("период обучения"):
                text_ref = text_ref or table_ref

            continue

        table_found = True

        if period_caption is not None and content_semester is None:
            report(
                "unexpected_format",
                f"не удалось прочитать период {period_caption!r}; "
                "нужно проверить принадлежность таблицы",
                table_ref,
            )
            continue

        if semesters is not None:
            if content_semester is None:
                report(
                    "manual_review",
                    "семестр таблицы не указан; темы прочитаны "
                    "без распределения по семестрам",
                    table_ref,
                )

            elif content_semester not in semesters:
                report(
                    "manual_review",
                    f"таблица относится к семестру {content_semester}, "
                    f"а выбранная нагрузка к семестрам {semesters}; "
                    "содержание не привязано",
                    table_ref,
                )
                continue

        rows = block.rows
        header = None

        for index, row in enumerate(rows[:5]):
            columns = course_content_columns(row.cells)

            if columns is not None:
                header = (index, columns, len(row.cells))
                break

        if header is None:
            wide_result = extract_wide_course_table(
                block,
                table_ref,
            )

            if wide_result is not None:
                wide_topics, wide_issues = wide_result
                topics.extend(wide_topics)
                issues.extend(wide_issues)
                continue

            report(
                "unexpected_format",
                "нет однозначной шапки с темой, видом занятий "
                "и количеством часов; таблица пока не разобрана",
                table_ref,
            )
            continue

        header_index, columns, width = header
        topic_start, issue_start = len(topics), len(issues)
        seen_cells = set()
        hour_owners = {}

        # Объединенные часы не распределяем между разными занятиями.
        for row in rows[header_index + 1:]:
            if len(row.cells) != width:
                continue

            cells = row.cells
            owner = (
                cells[columns["topic"]]._tc,
                cells[columns["activity"]]._tc,
            )
            hour_cell = cells[columns["hours"]]._tc
            hour_owners.setdefault(hour_cell, set()).add(owner)

        for row_number, row in enumerate(
            rows[header_index + 1:],
            start=header_index + 2,
        ):
            cells = row.cells
            row_ref = table_ref.model_copy(
                update={"row": row_number},
            )

            if not any(cell.text.strip() for cell in cells):
                continue

            repeated_header = course_content_columns(cells)

            if repeated_header is not None:
                if repeated_header != columns:
                    report(
                        "unexpected_format",
                        "внутри таблицы изменился порядок колонок; "
                        "оставшаяся часть требует проверки",
                        row_ref,
                    )
                    break

                continue

            if (
                len(cells) != width
                or row.grid_cols_before
                or row.grid_cols_after
            ):
                report(
                    "unexpected_format",
                    "строка не совпадает с сеткой шапки",
                    row_ref,
                )
                continue

            selected = {
                field: cells[index]
                for field, index in columns.items()
            }
            values = {
                field: " ".join(cell.text.split())
                for field, cell in selected.items()
            }

            if re.fullmatch(
                r"(?:итого|всего)(?:\s+по\b.*|\s*[:.]?)",
                normalize(values["topic"]),
            ):
                continue

            cell_key = tuple(
                selected[field]._tc
                for field in ("topic", "activity", "hours")
            )

            if cell_key in seen_cells:
                continue

            seen_cells.add(cell_key)

            if (
                len(set(cell_key)) != 3
                or len(hour_owners[selected["hours"]._tc]) > 1
            ):
                report(
                    "ambiguous",
                    "объединенная ячейка не позволяет однозначно "
                    "связать тему, вид занятия и часы",
                    row_ref,
                )
                continue

            if any(cell.tables for cell in selected.values()):
                report(
                    "unexpected_format",
                    "в ячейке обнаружена вложенная таблица",
                    row_ref,
                )
                continue

            missing = [
                field
                for field, value in values.items()
                if not value
            ]

            if missing:
                field = missing[0]

                report(
                    "empty_cell",
                    f"пустое поле {field}; строка {values!r} пропущена",
                    row_ref.model_copy(
                        update={"col": columns[field] + 1},
                    ),
                )
                continue

            try:
                hours = parse_workload_number(values["hours"])
            except ValueError:
                hours = None

            if hours is None or hours != hours.to_integral_value():
                report(
                    "unexpected_format",
                    f"часы {values['hours']!r} нельзя записать целым "
                    f"числом; тема {values['topic']!r}, "
                    f"вид занятия {values['activity']!r}",
                    row_ref.model_copy(
                        update={"col": columns["hours"] + 1},
                    ),
                )
                continue

            topics.append(CourseTopic(
                topic=values["topic"],
                activity=values["activity"],
                hours=int(hours),
                source_ref=row_ref.model_copy(
                    update={"col": columns["topic"] + 1},
                ),
            ))

        if len(topics) == topic_start and len(issues) == issue_start:
            report(
                "empty_cell",
                "в таблице нет строк тем с видом занятия и часами",
                table_ref,
            )

    if blocks and not table_found:
        report(
            "manual_review",
            "раздел содержит текст без поддержанной таблицы; "
            "темы с часами пока не извлечены",
            blocks[0][1],
        )

    elif table_found and text_ref is not None:
        report(
            "manual_review",
            "текстовые описания вне таблиц пока не перенесены "
            "в course_content",
            text_ref,
            severity="info",
        )

    return topics, issues
