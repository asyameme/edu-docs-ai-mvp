"""Название дисциплины, варианты нагрузки, часы, ЗЕТ и пререквизиты."""

import re

from docx.table import Table
from docx.text.paragraph import Paragraph

from .common import (
    heading_level,
    normalize,
    parse_workload_number,
    read_section,
)
from backend.contracts.document_v2.models import (
    Discipline,
    ParseIssue,
    SemesterWorkload,
    SourceRef,
)


def organization_tables(document, file_name):
    """Найти таблицы раздела организации занятий и его подразделов."""
    tables = []
    active = False
    depth = None
    section = None
    table_number = 0

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            if block.style.name.casefold().startswith("toc"):
                continue

            if re.search(r"\t\s*\d+\s*$", block.text):
                continue

            title = re.sub(
                r"^\d+(?:\.\d+)*\.?\s*",
                "",
                normalize(block.text).replace("\u0451", "е"),
            )
            level = heading_level(block)

            if title == "организация учебных занятий":
                active = True
                depth = level or 2
                section = block.text.strip()

            elif active and level is not None:
                if level <= depth:
                    break

                section = block.text.strip()

        elif isinstance(block, Table):
            table_number += 1

            if active:
                tables.append((
                    block,
                    SourceRef(
                        file=file_name,
                        section=section,
                        table=table_number,
                    ),
                ))

    return tables


def extract_control_forms(
    document,
    file_name,
    variant_rows=None,
    workload_variant=None,
    semesters=None,
):
    """Прочитать формы аттестации из соответствующих колонок."""
    forms = []
    issues = []
    found_columns = False

    pattern = re.compile(
        r"\b(?:дифференцированный\s+зачет|"
        r"зачет\s+с\s+оценкой|зачет|экзамен)\b"
    )

    for table, table_ref in organization_tables(document, file_name):
        rows = list(table.rows)
        columns = []
        seen = set()

        for row_index, row in enumerate(rows):
            labels = [
                normalize(cell.text).replace("\u0451", "е")
                for cell in row.cells
            ]

            candidates = [
                col
                for col, label in enumerate(labels)
                if label.startswith("виды промежуточной аттестации")
                or label.startswith("виды итоговой аттестации")
            ]

            if candidates:
                found_columns = True
                columns = candidates

                if row_index + 1 < len(rows):
                    lower = rows[row_index + 1].cells
                    columns = [
                        col
                        for col in columns
                        if col < len(lower)
                        and not normalize(
                            lower[col].text
                        ).startswith("срок")
                    ]

                continue

            if not columns or not re.fullmatch(
                r"семестр\s+\d+",
                labels[0],
            ):
                continue
            if variant_rows is not None and workload_variant is not None:
                actual = variant_rows.get(
                    (table_ref.table, row_index + 1),
                    (None, None),
                )

                if any(
                    value is not None
                    and expected is not None
                    and value != expected
                    for value, expected in zip(
                        actual,
                        workload_variant,
                    )
                ):
                    continue

            if semesters is not None:
                semester = int(
                    re.search(r"\d+", labels[0]).group()
                )

                if semester not in semesters:
                    continue
            for col in columns:
                if col >= len(row.cells):
                    continue

                cell = row.cells[col]

                if cell._tc in seen:
                    continue

                seen.add(cell._tc)
                text = normalize(cell.text).replace("\u0451", "е")

                if not text or text in {"-", "\u2013", "\u2014"}:
                    continue

                ref = table_ref.model_copy(update={
                    "row": row_index + 1,
                    "col": col + 1,
                })

                matches = list(pattern.finditer(text))
                negative = re.search(
                    r"\bне\s+(?:предусмотрен\w*|проводится|проводятся)\b",
                    text,
                )

                if not matches or negative:
                    issues.append(ParseIssue(
                        category="manual_review",
                        message=(
                            "Не удалось однозначно разобрать "
                            f"форму контроля: {cell.text}"
                        ),
                        source_ref=ref,
                    ))
                    continue

                for match in matches:
                    form = match.group()

                    if form not in forms:
                        forms.append(form)

    if not found_columns or not forms and not issues:
        issues.append(ParseIssue(
            category="not_found",
            message=(
                "Не найдены формы промежуточной "
                "или итоговой аттестации"
            ),
            source_ref=SourceRef(file=file_name),
        ))

    return forms, issues


def organization_variant_rows(document, file_name):
    """Связать строки раздела 2.1 с траекторией и формой обучения."""
    variants = {}
    cells_by_ref = {}
    cell_variants = {}

    for table, table_ref in organization_tables(document, file_name):
        track, mode = None, None

        for row_number, row in enumerate(table.rows, 1):
            cells = row.cells

            if cells and len({cell._tc for cell in cells}) == 1:
                text = normalize(cells[0].text)

                if text.startswith((
                    "основная траектория",
                    "траектория",
                )):
                    track, mode = text, None

                match = re.fullmatch(
                    r"(?:форма обучения\s*:\s*)?"
                    r"(очная|заочная|очно-заочная)"
                    r"(?: форма обучения)?",
                    text,
                )

                if match:
                    mode = match.group(1)

            variant = (track, mode)
            variants[(table_ref.table, row_number)] = variant

            for column, cell in enumerate(cells, 1):
                ref = (table_ref.table, row_number, column)
                cells_by_ref[ref] = cell._tc
                cell_variants.setdefault(cell._tc, set()).add(
                    variant
                )

    conflicts = {
        ref
        for ref, cell in cells_by_ref.items()
        if len(cell_variants[cell]) > 1
    }

    return variants, conflicts


def select_workload_variant(
    data,
    variants,
    conflicts,
    workload_track=None,
    education_mode=None,
):
    """Выбрать один явно указанный вариант нагрузки."""
    groups = {}

    for item in data:
        ref = item["source_ref"]
        variant = variants.get(
            (ref.table, ref.row),
            (None, None),
        )
        groups.setdefault(variant, []).append(item)

    tracks = {
        track
        for track, mode in variants.values()
        if track is not None
    }
    modes = {
        mode
        for track, mode in variants.values()
        if mode is not None
    }

    wanted_track = (
        normalize(workload_track)
        if workload_track is not None
        else None
    )
    wanted_mode = (
        normalize(education_mode)
        if education_mode is not None
        else None
    )

    if wanted_track == "":
        raise ValueError("workload_track не должен быть пустым")

    if wanted_mode not in {
        None, "очная", "заочная", "очно-заочная",
    }:
        raise ValueError("Неизвестная форма обучения")

    available = "; ".join(
        f"траектория: {track or 'без метки'}, "
        f"форма: {mode or 'без метки'}"
        for track, mode in groups
    )

    def issue(category, message):
        return [], None, [ParseIssue(
            category=category,
            message=(
                f"{message}. "
                f"Доступные варианты: {available}"
            ),
            source_ref=data[0]["source_ref"],
        )]

    if workload_track is None and education_mode is None:
        if len(tracks) > 1 or len(modes) > 1:
            return issue(
                "ambiguous",
                "Найдено несколько траекторий или форм обучения; "
                "нужно явно выбрать вариант нагрузки",
            )

    # Не распределяем значения без метки между размеченными блоками.
    for axis, wanted in enumerate((wanted_track, wanted_mode)):
        labels = {variant[axis] for variant in groups}

        if wanted is not None and None in labels and len(labels) > 1:
            return issue(
                "ambiguous",
                "Часть нагрузки не имеет метки варианта; "
                "ее принадлежность не определена",
            )

    candidates = [
        variant
        for variant in groups
        if (
            wanted_track is None
            or not tracks
            or variant[0] == wanted_track
        )
        and (
            wanted_mode is None
            or not modes
            or variant[1] == wanted_mode
        )
    ]

    if len(candidates) != 1:
        return issue(
            "not_found" if not candidates else "ambiguous",
            "Не удалось однозначно выбрать вариант нагрузки",
        )

    chosen = candidates[0]
    selected = groups[chosen]

    if any(
        (
            item["source_ref"].table,
            item["source_ref"].row,
            item["source_ref"].col,
        ) in conflicts
        for item in selected
    ):
        return issue(
            "ambiguous",
            "Ячейка нагрузки объединена между разными вариантами",
        )

    issues = []

    if workload_track is not None or education_mode is not None:
        issues.append(ParseIssue(
            category="manual_review",
            severity="info",
            message=(
                "Выбран вариант нагрузки: траектория "
                f"{chosen[0] or 'без метки'}, форма "
                f"{chosen[1] or 'без метки'}"
            ),
            source_ref=selected[0]["source_ref"],
        ))

    return selected, chosen, issues


def build_discipline(
    document,
    file_name,
    title_parts,
    workload_track=None,
    education_mode=None,
):
    """Собрать одну дисциплину из титула и таблиц нагрузки."""
    values, issues = extract_workload(document, file_name)
    name = extract_discipline_name(title_parts)

    def problem(message, ref, category="manual_review"):
        issues.append(ParseIssue(
            category=category,
            message=message,
            source_ref=ref,
        ))

    if not name:
        problem(
            "Не удалось однозначно прочитать название дисциплины с титула",
            SourceRef(file=file_name, section="титул"),
        )

    data = [
        item
        for item in values
        if item["kind"] in {"hours", "hours_total", "credit_units"}
    ]

    if not name or not data:
        return None, issues

    variant_rows, variant_conflicts = organization_variant_rows(
        document,
        file_name,
    )

    data, chosen_variant, variant_issues = select_workload_variant(
        data,
        variant_rows,
        variant_conflicts,
        workload_track=workload_track,
        education_mode=education_mode,
    )
    issues.extend(variant_issues)

    if not data:
        return None, issues

    if len({item["source_ref"].table for item in data}) != 1:
        problem(
            "В выбранном варианте несколько таблиц нагрузки; "
            "их нельзя автоматически объединить",
            data[0]["source_ref"],
        )
        return None, issues

    semesters = set()
    total_labels = {"итого", "всего"}

    for item in data:
        for period in item["periods"]:
            text = normalize(period)

            if text in total_labels:
                continue

            match = re.fullmatch(r"семестр\s+([1-8])", text)

            if not match:
                problem(
                    f"Не удалось прочитать семестр из {period!r}",
                    item["source_ref"],
                    "unexpected_format",
                )
                return None, issues

            semesters.add(int(match.group(1)))

    if not semesters:
        problem(
            "Не найдены семестры дисциплины",
            data[0]["source_ref"],
        )
        return None, issues

    totals = [
        item
        for item in data
        if any(
            normalize(period) in total_labels
            for period in item["periods"]
        )
    ]
    selected = totals or data

    if totals and len({
        item["source_ref"].row
        for item in totals
    }) != 1:
        problem(
            "Найдено несколько итоговых строк нагрузки",
            totals[0]["source_ref"],
            "ambiguous",
        )
        return None, issues

    first_ref = selected[0]["source_ref"]
    primary_ref = first_ref.model_copy(update={"col": None})

    canonical_hours = {
        text.replace(" ", ""): text
        for text in (
            "лекции",
            "семинары",
            "консультации",
            "практические занятия",
            "лабораторные работы",
            "контрольные работы",
            "коллоквиумы",
            "текущий контроль",
            "промежуточная аттестация",
            "итоговая аттестация",
            "под руководством преподавателя",
            "в присутствии преподавателя",
            "сам.раб. с использованием методических материалов",
            "текущий контроль (сам.раб.)",
            "промежуточная аттестация (сам.раб.)",
            "итоговая аттестация (сам.раб.)",
            "в т. ч. с использованием учебно-методич. материалов",
        )
    }

    hours_by_kind = {}
    credits = 0
    credit_found = False
    declared_hours = []
    seen = set()

    for item in selected:
        value = item["value"]
        ref = item["source_ref"]

        if (
            value is None
            or value < 0
            or value != value.to_integral_value()
        ):
            problem(
                f"Значение {item['raw']!r} нельзя перенести "
                "в целочисленное поле модели",
                ref,
                "unexpected_format",
            )
            return None, issues

        periods = {
            normalize(period)
            for period in item["periods"]
            if normalize(period) not in total_labels
        }

        if not totals and len(periods) != 1:
            problem(
                "Значение объединено для нескольких семестров; "
                "его объем неоднозначен",
                ref,
                "ambiguous",
            )
            return None, issues

        key = normalize(item["label"]).replace("\u0451", "е")
        key = re.sub(r"сам\.\s*раб\.", "сам.раб.", key)
        group = normalize(item["group"])

        if item["kind"] == "hours":
            canonical = canonical_hours.get(
                key.replace(" ", "")
            )

            if canonical is None:
                problem(
                    f"Колонка {item['label']!r} не входит "
                    "в правила суммирования часов",
                    ref,
                )
                return None, issues

            key = canonical

            if not group.startswith((
                "контактная работа",
                "аудиторная работа",
                "самостоятельная работа",
            )):
                problem(
                    "Неизвестная группа часов в шапке",
                    ref,
                )
                return None, issues

            if group.startswith("самостоятельная работа") and (
                "сам.раб." not in key
                and "самостоятельн" not in key
            ):
                key += " (сам.раб.)"

        else:
            key = item["kind"]

        scope = (
            ("итого",)
            if totals
            else tuple(sorted(periods))
        )
        signature = (key, scope)

        if signature in seen:
            problem(
                f"Показатель {key!r} повторяется в одном периоде",
                ref,
                "ambiguous",
            )
            return None, issues

        seen.add(signature)
        number = int(value)

        if item["kind"] == "credit_units":
            credits += number
            credit_found = True

        elif item["kind"] == "hours_total":
            declared_hours.append((number, periods))

        else:
            hours_by_kind[key] = (
                hours_by_kind.get(key, 0) + number
            )

    if not credit_found or credits < 1 or not hours_by_kind:
        problem(
            "Недостаточно данных о ЗЕТ или часах "
            "для создания Discipline",
            primary_ref,
            "empty_cell",
        )
        return None, issues

    if declared_hours:
        covered = set().union(*(
            periods
            for number, periods in declared_hours
        ))

        if not totals and covered != {
            f"семестр {semester}"
            for semester in semesters
        }:
            problem(
                "Общие часы указаны не для всех семестров",
                primary_ref,
            )
            return None, issues

        hours_total = sum(
            number
            for number, periods in declared_hours
        )

    else:
        hours_total = sum(hours_by_kind.values())

        issues.append(ParseIssue(
            category="manual_review",
            severity="info",
            message=(
                "hours_total вычислен как сумма извлеченных "
                "компонентов нагрузки; отдельного значения "
                "общих часов в таблице нет"
            ),
            source_ref=primary_ref,
        ))

    if len(semesters) > 1:
        problem(
            "В Discipline сохранена общая нагрузка. "
            "Текущая модель не передает часы и ЗЕТ "
            "отдельно для каждого семестра",
            primary_ref,
        )

    forms, form_issues = extract_control_forms(
        document,
        file_name,
        variant_rows=(
            variant_rows
            if workload_track is not None or education_mode is not None
            else None
        ),
        workload_variant=chosen_variant,
        semesters=(
            semesters
            if workload_track is not None or education_mode is not None
            else None
        ),
    )
    issues.extend(form_issues)

    discipline = Discipline(
        name=name,
        semesters=sorted(semesters),
        credit_units=credits,
        hours_total=hours_total,
        hours_by_kind=hours_by_kind,
        control_forms=forms,
        source_ref=primary_ref.model_copy(update={
            "section": f"титул; {primary_ref.section}",
        }),
    )

    return discipline, issues


def extract_workload(document, file_name):
    """Прочитать значения нагрузки, сохранив их назначение и координаты."""
    blocks = list(document.iter_inner_content())
    values, issues, starts = [], [], []
    section = None
    table_number = None

    def report(category, message, row=None, col=None, ref=None):
        issues.append(ParseIssue(
            category=category,
            message=message,
            source_ref=ref or SourceRef(
                file=file_name,
                section=section,
                table=table_number,
                row=row,
                col=col,
            ),
        ))

    for index, block in enumerate(blocks):
        if not isinstance(block, Paragraph):
            continue

        if block.style.name.casefold().startswith("toc"):
            continue

        if re.search(r"\t\s*\d+\s*$", block.text):
            continue

        title = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*",
            "",
            normalize(block.text).replace("\u0451", "е"),
        )

        if title == "организация учебных занятий":
            starts.append(index)

    if len(starts) != 1:
        report(
            "not_found" if not starts else "ambiguous",
            "Не удалось однозначно найти раздел организации занятий",
        )
        return values, issues

    start = starts[0]
    section = blocks[start].text.strip()
    depth = heading_level(blocks[start]) or 2

    table_number = sum(
        isinstance(block, Table)
        for block in blocks[:start]
    )

    root_ref = SourceRef(file=file_name, section=section)
    found_table = False
    workload_ref = root_ref

    for block in blocks[start + 1:]:
        if isinstance(block, Paragraph):
            level = heading_level(block)

            if level is not None:
                if level <= depth:
                    break

                section = block.text.strip()

            continue

        if not isinstance(block, Table):
            continue

        table_number += 1
        rows = list(block.rows)
        header_rows = []

        for index, row in enumerate(rows):
            labels = [
                normalize(cell.text).replace("\u0451", "е")
                for cell in row.cells
            ]

            if "лекции" in labels and any(
                label.startswith("трудоемк")
                for label in labels
            ):
                header_rows.append(index)

        if not header_rows:
            continue

        found_table = True
        workload_ref = SourceRef(
            file=file_name,
            section=section,
            table=table_number,
        )

        if len(header_rows) != 1 or header_rows[0] == 0:
            report(
                "unexpected_format",
                "Не удалось однозначно прочитать шапку нагрузки",
            )
            continue

        header_index = header_rows[0]
        header_cells = list(rows[header_index].cells)

        headers = [
            " ".join(cell.text.split())
            for cell in header_cells
        ]
        groups = [
            " ".join(cell.text.split())
            for cell in rows[header_index - 1].cells
        ]

        cells_by_id = {}
        seen_periods = set()

        for row_number, row in enumerate(
            rows[header_index + 1:],
            header_index + 2,
        ):
            cells = list(row.cells)
            period = " ".join(cells[0].text.split())

            # В одном Word-объекте могут идти две разные таблицы.
            if "текущего контроля" in normalize(period):
                break

            if not re.match(
                r"^(семестр\b|итого\b|всего\b)",
                period,
                re.IGNORECASE,
            ):
                if any(
                    re.fullmatch(
                        r"\d+(?:[.,]\d+)?",
                        cell.text.strip(),
                    )
                    for cell in cells[1:]
                ):
                    report(
                        "manual_review",
                        f"Не распознан период нагрузки {period!r}",
                        row_number,
                    )
                continue

            if (
                len(cells) != len(headers)
                or len(groups) != len(headers)
            ):
                report(
                    "unexpected_format",
                    "Число ячеек не соответствует шапке нагрузки",
                    row_number,
                )
                continue

            covered_headers = {}

            for col, cell in enumerate(cells):
                if cell.text.strip():
                    covered_headers.setdefault(
                        cell._tc, set()
                    ).add(header_cells[col]._tc)

            if any(
                len(items) > 1
                for items in covered_headers.values()
            ):
                report(
                    "manual_review",
                    "Разметка ячеек строки не совпадает с шапкой нагрузки",
                    row_number,
                )
                continue

            new_hour_values = [
                cell.text.strip()
                for col, cell in enumerate(cells[1:], 1)
                if cell.text.strip()
                and cell._tc not in cells_by_id
                and "трудо" not in normalize(headers[col])
                and "активн" not in normalize(headers[col])
                and "интерактивн" not in normalize(headers[col])
            ]

            capacity_row = (
                cells[0]._tc in seen_periods
                and bool(new_hour_values)
                and all(
                    re.fullmatch(
                        r"\d+\s*[-\u2013\u2014]\s*\d+",
                        value,
                    )
                    for value in new_hour_values
                )
                and "наполняемость" in normalize(
                    rows[0].cells[0].text
                )
            )

            seen_periods.add(cells[0]._tc)

            for col, cell in enumerate(cells[1:], 1):
                raw = cell.text.strip()
                label = headers[col]
                key = normalize(label).replace("\u0451", "е")

                kind = (
                    "credit_units"
                    if key.startswith("трудоемк") and "час" not in key
                    else "hours_total"
                    if key.startswith("трудоемк")
                    else "reference"
                    if "активн" in key or "интерактивн" in key
                    else "capacity"
                    if capacity_row
                    else "hours"
                )

                if not raw:
                    if kind == "credit_units":
                        report(
                            "empty_cell",
                            "Не указана трудоемкость в ЗЕТ",
                            row_number,
                            col + 1,
                        )
                    continue

                if cell._tc in cells_by_id:
                    previous = cells_by_id[cell._tc]

                    if period not in previous["periods"]:
                        previous["periods"].append(period)

                    continue

                value = None

                if kind != "capacity":
                    try:
                        value = parse_workload_number(raw)
                    except ValueError:
                        report(
                            "unexpected_format",
                            f"Не удалось прочитать число {raw!r}",
                            row_number,
                            col + 1,
                        )

                item = {
                    "periods": [period],
                    "group": groups[col],
                    "label": label,
                    "kind": kind,
                    "raw": raw,
                    "value": value,
                    "source_ref": SourceRef(
                        file=file_name,
                        section=section,
                        table=table_number,
                        row=row_number,
                        col=col + 1,
                    ),
                }

                cells_by_id[cell._tc] = item
                values.append(item)

    if not found_table:
        report(
            "not_found",
            "Не найдена таблица нагрузки с колонками лекций и трудоемкости",
            ref=root_ref,
        )
    elif not values and not issues:
        report(
            "unparsed_row",
            "В таблице нагрузки не найдены распознаваемые значения",
            ref=workload_ref,
        )

    return values, issues


def extract_discipline_name(title_parts):
    """Прочитать название после слов 'учебной дисциплины'."""
    parts = [
        " ".join(text.split())
        for text in title_parts
        if text.strip()
    ]
    markers = []

    for index, text in enumerate(parts):
        match = re.search(
            r"\bучебной\s+дисциплины\b\s*:?\s*",
            text,
            re.IGNORECASE,
        )
        if match:
            markers.append((index, match.end()))

    if len(markers) != 1:
        return None

    index, end = markers[0]
    name = parts[index][end:].strip()

    if not name and index + 1 < len(parts):
        name = parts[index + 1]

    if normalize(name).replace("\u0451", "е").startswith((
        "язык(и) обучения",
        "языки обучения",
        "трудоемкость",
        "регистрационный номер",
        "санкт-петербург",
    )):
        return None

    return name.strip(' «»"') or None


def extract_prerequisites(document, file_name, *, doc_type="RPD"):
    """Извлечь явно перечисленные названия дисциплин."""
    if doc_type not in {"RPD", "RPP"}:
        raise ValueError("Поддерживаются РПД и РПП")

    def is_heading(paragraph):
        title = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*",
            "",
            normalize(paragraph.text).replace("\u0451", "е"),
        )

        if doc_type == "RPP":
            return re.fullmatch(
                r"требования (?:к )?подготовленности к прохождению практики",
                title.rstrip(".:"),
            ) is not None

        return title == "пререквизиты" or re.fullmatch(
            r"требования (?:к )?подготовленности обучающегося "
            r"к освоению содержания учебных занятий"
            r"(?: \(пререквизиты\))?",
            title,
        ) is not None

    blocks, issues = read_section(
        document,
        file_name,
        is_heading,
        "пререквизиты",
    )

    records = []
    found_marker = False

    absence = re.compile(
        r"\b(?:пререквизиты|предшествующие дисциплины)\s*:?\s*"
        r"(?:не требуются|не предусмотрены|отсутствуют)\b"
        r"|\bпредшествующих дисциплин нет\b",
        re.IGNORECASE,
    )

    absence_ref = next((
        ref
        for block, ref in blocks
        if isinstance(block, Paragraph)
        and absence.search(" ".join(block.text.split()))
    ), None)

    quote = r'(?:«[^«»]+»|"[^"]+")'
    separator = (
        r"(?:\s*[,;]\s*(?:(?:и|или|либо)\s+)?"
        r"|\s+(?:и|или|либо)\s+)"
    )
    quoted_list = re.compile(
        rf"{quote}(?:{separator}{quote})*"
    )

    markers = re.compile(
        r"(?:^|(?<=[.!?])\s+)освоение\s+дисциплин(?:ы)?\b\s*:?\s*"
        r"|\bпри\s+изучении\s+(?:учебн(?:ых|ой)\s+)?"
        r"дисциплин(?:ы)?\b\s*:?\s*"
        r"|\bизучени[яи]\s+предшествующ[а-я]+\s+"
        r"(?:учебн(?:ых|ой)\s+)?дисциплин(?:ы)?\b"
        r"(?:\s+ООП)?(?:\s+таких,\s*как)?\s*:?\s*"
        r"|\bпо\s+(?:дисциплинам|курсам|курсу)\s*"
        r"(?::\s*|(?=[«\"]))"
        r"|\b(?:рекомендуемые\s+)?предшествующие\s+"
        r"дисциплины\s*:\s*",
        re.IGNORECASE,
    )

    for block, ref in blocks:
        if isinstance(block, Table):
            issues.append(ParseIssue(
                category="manual_review",
                message=(
                    "Табличный формат пререквизитов "
                    "пока не поддержан"
                ),
                source_ref=ref,
            ))
            continue

        text = " ".join(block.text.split())

        for marker in markers.finditer(text):
            if any(
                marker.start() < item.end()
                and item.start() < marker.end()
                for item in absence.finditer(text)
            ):
                continue

            found_marker = True
            body = text[marker.end():].strip()
            names = []
            rest = ""

            if body.startswith(("«", '"')):
                if (
                    body.count("«") != body.count("»")
                    or body.count('"') % 2
                ):
                    issues.append(ParseIssue(
                        category="unexpected_format",
                        message=(
                            "В перечне дисциплин "
                            "нарушены парные кавычки"
                        ),
                        source_ref=ref,
                    ))
                    continue

                match = quoted_list.match(body)

                if match:
                    names = [
                        part.group()[1:-1].strip()
                        for part in re.finditer(
                            quote, match.group()
                        )
                    ]
                    rest = body[match.end():].strip()

                continuation = re.sub(
                    r"^[,;]\s*", "", rest
                )
                unclear_tail = (
                    bool(rest)
                    and not re.match(
                        r"^(?:[.!?]|котор[а-я]*\b|"
                        r"(?:или|либо)\s+эквивалентн)",
                        continuation,
                        re.IGNORECASE,
                    )
                )

                if unclear_tail:
                    issues.append(ParseIssue(
                        category="manual_review",
                        message=(
                            "Не удалось полностью разобрать "
                            "перечень дисциплин"
                        ),
                        source_ref=ref,
                    ))

            else:
                if not re.fullmatch(
                    r"\s*(?:освоение дисциплин(?:ы)?|"
                    r"(?:рекомендуемые )?"
                    r"предшествующие дисциплины)\s*:\s*",
                    marker.group(),
                    re.IGNORECASE,
                ):
                    issues.append(ParseIssue(
                        category="manual_review",
                        message=(
                            "Названия в описании подготовки "
                            "не выделены кавычками "
                            "или явным списком"
                        ),
                        source_ref=ref,
                    ))
                    continue

                sentence = re.split(
                    r"\.(?=\s|$)",
                    body,
                    maxsplit=1,
                )[0].strip(" ;")

                if not re.search(
                    r'[«»"“”„:]|\b(?:должен|необходимо|'
                    r'требуется|которые|обучающийся|студент)\b',
                    sentence,
                    re.IGNORECASE,
                ) and not re.match(
                    r"^(?:не\b|нет\b|отсутствуют\b)",
                    sentence,
                    re.IGNORECASE,
                ):
                    names = [
                        name.strip()
                        for name in re.split(
                            r"[,;]", sentence
                        )
                    ]

            if not names or any(not name for name in names):
                issues.append(ParseIssue(
                    category="manual_review",
                    message=(
                        "Не удалось выделить названия "
                        "после указания пререквизитов"
                    ),
                    source_ref=ref,
                ))
                continue

            for name in names:
                records.append({
                    "name": " ".join(name.split()),
                    "source_ref": ref,
                })

            if (
                "рекомендуемые" in normalize(marker.group())
                or re.search(
                    r"\b(?:или|либо)\s+эквивалентн",
                    rest,
                    re.IGNORECASE,
                )
                or re.search(
                    r'(?:»|")\s+(?:или|либо)\s+(?:«|")',
                    body,
                    re.IGNORECASE,
                )
            ):
                issues.append(ParseIssue(
                    category="manual_review",
                    severity="info",
                    message=(
                        "В источнике список рекомендован "
                        "или допускает альтернативы; prerequisites "
                        "сохраняет названия без признака обязательности"
                    ),
                    source_ref=ref,
                ))

    if absence_ref is not None and found_marker:
        issues.append(ParseIssue(
            category="ambiguous",
            message=(
                "В разделе одновременно есть перечень "
                "и указание отсутствия пререквизитов"
            ),
            source_ref=absence_ref,
        ))

    if (
        blocks
        and not found_marker
        and not issues
        and absence_ref is None
    ):
        issues.append(ParseIssue(
            category="manual_review",
            severity="info",
            message=(
                "Раздел подготовки прочитан, но явный перечень "
                "дисциплин не распознан; требования к знаниям "
                "не преобразованы в названия"
            ),
            source_ref=blocks[0][1],
        ))

    return records, issues

def extract_practice_workload(document, file_name):
    """Прочитать общую и посеместровую нагрузку из текста РПП."""
    def is_heading(paragraph):
        text = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*",
            "",
            normalize(paragraph.text),
        )
        return bool(re.fullmatch(
            r"организация практики(?:\s*:.*)?",
            text,
        ))

    blocks, issues = read_section(
        document,
        file_name,
        is_heading,
        "Организация практики",
    )

    if not blocks:
        return [], issues

    records = []
    plan_ref = None

    amount_pattern = (
        r"(.+?)\s*(?:з\.?\s*е\.?|зачетных\s+единиц|зет)"
        r"\s*\(\s*(.+?)\s+(?:академических\s+)?час(?:ов|а)?\s*\)\.?"
    )

    for block, ref in blocks:
        if not isinstance(block, Paragraph):
            issues.append(ParseIssue(
                category="manual_review",
                message="Таблица нагрузки РПП требует отдельного разбора",
                source_ref=ref,
            ))
            continue

        raw = " ".join(block.text.split())
        text = normalize(raw).replace("\u0451", "е")

        if "учебном плане" in text:
            plan_ref = ref

        semester_match = re.fullmatch(
            r"семестр\s+(\d+)\s*:\s*(.*)",
            text,
        )
        total_match = re.fullmatch(
            r"общая\s+трудоемкость(?:\s+практики)?"
            r"(?:\s+составляет)?\s*:?\s*(.*)",
            text,
        )

        if semester_match:
            scope = "semester"
            semester = int(semester_match.group(1))
            amount = semester_match.group(2)

        elif total_match:
            scope = "total"
            semester = None
            amount = total_match.group(1)

        else:
            if re.match(r"семестр\b", text):
                issues.append(ParseIssue(
                    category="unparsed_row",
                    message=f"Не разобрана строка нагрузки: {raw}",
                    source_ref=ref,
                ))
            continue

        record = {
            "scope": scope,
            "semester": semester,
            "credit_units": None,
            "hours_total": None,
            "raw": raw,
            "source_ref": ref,
        }

        match = re.fullmatch(amount_pattern, amount)

        if match:
            try:
                record["credit_units"] = parse_workload_number(
                    match.group(1)
                )
                record["hours_total"] = parse_workload_number(
                    match.group(2)
                )
            except ValueError:
                pass

        if (
            record["credit_units"] is None
            or record["hours_total"] is None
        ):
            issues.append(ParseIssue(
                category="unexpected_format",
                message=(
                    "Не удалось прочитать нагрузку "
                    f"без предположений: {raw}"
                ),
                source_ref=ref,
            ))

        records.append(record)

    if not records:
        issues.append(ParseIssue(
            category="manual_review" if plan_ref else "not_found",
            message=(
                "Локальные значения нагрузки не найдены; "
                "документ отсылает к УП"
                if plan_ref
                else "В разделе не найдены значения нагрузки"
            ),
            source_ref=plan_ref or blocks[0][1],
        ))

    return records, issues


def build_practice_discipline(document, file_name, title_parts):
    """Собрать практику и прочитанную посеместровую нагрузку."""
    records, issues = extract_practice_workload(document, file_name)

    if not records:
        return None, [], issues

    totals = [
        item for item in records
        if item["scope"] == "total"
    ]
    periods = [
        item for item in records
        if item["scope"] == "semester"
    ]
    ref = records[0]["source_ref"]

    if len(totals) != 1:
        issues.append(ParseIssue(
            category="ambiguous" if totals else "not_found",
            message="Не удалось однозначно прочитать общий объем практики",
            source_ref=ref,
        ))
        return None, [], issues

    total = totals[0]

    for field, minimum in (
        ("credit_units", 1),
        ("hours_total", 0),
    ):
        value = total[field]

        if value is None:
            return None, [], issues

        if value < minimum or value != value.to_integral_value():
            issues.append(ParseIssue(
                category="unexpected_format",
                message=(
                    f"Общее значение {field}={value} "
                    "не подходит модели"
                ),
                source_ref=total["source_ref"],
            ))
            return None, [], issues

    semesters = [item["semester"] for item in periods]

    if (
        not semesters
        or any(not 1 <= value <= 8 for value in semesters)
        or len(semesters) != len(set(semesters))
    ):
        issues.append(ParseIssue(
            category="manual_review",
            message=(
                "Семестры практики отсутствуют, повторяются "
                "или выходят за 1-8"
            ),
            source_ref=ref,
        ))
        return None, [], issues

    parts = [
        " ".join(text.split())
        for text in title_parts
        if text.strip()
    ]
    names = []

    for index, text in enumerate(parts):
        compact = re.sub(
            r"\s+", "", text,
        ).casefold().rstrip(":")

        if compact in {
            "практики",
            "рабочаяпрограммапрактики",
        }:
            if index + 1 < len(parts):
                names.append(parts[index + 1].strip(' «»"'))

    if (
        len(names) != 1
        or normalize(names[0]).replace("\u0451", "е").startswith((
            "язык",
            "трудоемкость",
            "регистрационный",
            "аннотация",
        ))
    ):
        issues.append(ParseIssue(
            category="manual_review",
            message="Не удалось прочитать название практики с титула",
            source_ref=SourceRef(file=file_name, section="титул"),
        ))
        return None, [], issues

    semester_workload = []
    for item in periods:
        credits, hours = item["credit_units"], item["hours_total"]
        if credits is None or hours is None:
            continue  # Причина уже записана при извлечении.
        if credits != credits.to_integral_value() or hours != hours.to_integral_value():
            issues.append(ParseIssue(
                category="unexpected_format",
                message="В контракте v2 нужны целые ЗЕТ и часы: " + item["raw"],
                source_ref=item["source_ref"],
            ))
            continue
        semester_workload.append(SemesterWorkload(
            semester=item["semester"],
            credit_units=int(credits),
            hours_total=int(hours),
            source_ref=item["source_ref"],
        ))

    discipline = Discipline(
        name=names[0],
        semesters=sorted(semesters),
        credit_units=int(total["credit_units"]),
        hours_total=int(total["hours_total"]),
        hours_by_kind={},
        source_ref=total["source_ref"].model_copy(update={
            "section": f"титул; {total['source_ref'].section}",
        }),
    )

    return discipline, semester_workload, issues


def extract_practice_control_forms(document, file_name):
    """Прочитать явно выбранные формы промежуточной аттестации РПП."""
    def is_heading(paragraph):
        text = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*", "", normalize(paragraph.text),
        )
        return text.startswith("виды и формы текущего контроля")

    blocks, issues = read_section(
        document, file_name, is_heading, "формы контроля практики",
        keep_numbered_items=True,
    )
    if not blocks:
        return [], issues

    forms = []
    active = False
    form_pattern = (
        r"\b(?:дифференцированный\s+зачет|зачет\s+с\s+оценкой|зачет|экзамен)\b"
    )
    checked = "🗹☑☒✓✔\uf0fe"
    unchecked = "□☐"
    marked_pattern = re.compile(
        rf"([{checked}{unchecked}])\s*({form_pattern})"
    )

    for block, ref in blocks:
        if not isinstance(block, Paragraph):
            issues.append(ParseIssue(
                category="manual_review",
                message="Табличные формы контроля практики требуют проверки",
                source_ref=ref,
            ))
            continue

        text = normalize(block.text).replace("\u0451", "е")
        if re.search(r"промежуточн\w*\s+аттестац", text):
            active = True
        if not active:
            continue

        candidates = re.findall(form_pattern, text)
        if not candidates:
            continue

        marked = list(marked_pattern.finditer(text))
        selected = [
            match.group(2) for match in marked if match.group(1) in checked
        ]
        if not marked and not any(char in text for char in checked + unchecked):
            if len(candidates) == 1 and not re.search(
                r"\bне\s+(?:предусмотрен\w*|проводится)\b", text,
            ):
                selected = candidates

        if not selected:
            issues.append(ParseIssue(
                category="manual_review",
                message=(
                    "Не удалось определить отмеченную форму аттестации: "
                    + " ".join(block.text.split())
                ),
                source_ref=ref,
            ))
            continue

        for form in selected:
            if form not in forms:
                forms.append(form)

    if not forms and not issues:
        issues.append(ParseIssue(
            category="empty_cell",
            message="Не найдена явно указанная форма аттестации практики",
            source_ref=blocks[0][1],
        ))
    return forms, issues
