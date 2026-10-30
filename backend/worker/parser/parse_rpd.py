import argparse
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from decimal import Decimal

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
    LearningOutcome,
    Discipline,
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
def extract_control_forms(document, file_name):
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

def build_discipline(document, file_name, title_parts):
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

    if len({
        item["source_ref"].table
        for item in data
    }) != 1:
        problem(
            "Найдено несколько таблиц нагрузки; "
            "их нельзя автоматически объединить",
            SourceRef(file=file_name),
        )
        return None, issues

    tracks = set()
    modes = set()

    for table, table_ref in organization_tables(document, file_name):
        for row in table.rows:
            cells = list(row.cells)

            if len({cell._tc for cell in cells}) != 1:
                continue

            text = normalize(cells[0].text)

            if text.startswith((
                "основная траектория",
                "траектория",
            )):
                tracks.add(text)

            mode = re.fullmatch(
                r"(?:форма обучения\s*:\s*)?"
                r"(очная|заочная|очно-заочная)"
                r"(?: форма обучения)?",
                text,
            )

            if mode:
                modes.add(mode.group(1))

    if len(tracks) > 1 or len(modes) > 1:
        problem(
            "Найдено несколько траекторий или форм обучения; "
            "для Discipline нужно выбрать соответствующий блок",
            data[0]["source_ref"],
            "ambiguous",
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

def extract_learning_outcomes(text, source_ref, start_index=1):
    """Разобрать результаты обучения одной ячейки."""
    outcomes = []
    issues = []
    buffer = []
    current_kind = None

    kinds = {
        "знает": "знать", "знать": "знать",
        "умеет": "уметь", "уметь": "уметь",
        "владеет": "владеть", "владеть": "владеть",
    }
    code_kinds = {
        "З": "знать",
        "У": "уметь",
        "В": "владеть",
    }

    heading_pattern = re.compile(
        r"^(знает|знать|умеет|уметь|владеет|владеть)\b\s*:?\s*",
        re.IGNORECASE,
    )
    code_pattern = (
        r"(?P<code>[А-ЯA-Z][А-ЯA-Z0-9]*"
        r"(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+\.?\s*"
        r"(?P<kind>[ЗУВ3])-\d+)"
    )
    prefix_pattern = re.compile(
        rf"^{code_pattern}[.:\s\u2013\u2014-]*(?P<text>.*)$"
    )
    suffix_pattern = re.compile(
        rf"^(?P<text>.+?)\s*\({code_pattern}\)[.;]?$"
    )

    def add_outcome(value, kind, code=None, message=None):
        value = " ".join(value.split())

        if not value:
            issues.append(ParseIssue(
                category="empty_cell",
                message=(
                    f"Нет описания результата обучения {code or ''}"
                ).strip(),
                source_ref=source_ref,
            ))
            return

        outcomes.append(LearningOutcome(
            local_id=f"LO-{start_index + len(outcomes):03d}",
            text=value,
            kind=kind,
            code=code,
            source_ref=source_ref,
        ))

        if message or kind is None:
            issues.append(ParseIssue(
                category="manual_review",
                message=message or (
                    "Не удалось определить вид результата обучения; "
                    "текст сохранен"
                ),
                source_ref=source_ref,
            ))

    def flush():
        if buffer:
            add_outcome(" ".join(buffer), current_kind)
            buffer.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("•●▪").strip()

        if not line:
            continue

        heading = heading_pattern.match(line)

        if heading:
            flush()
            current_kind = kinds[heading.group(1).casefold()]
            line = line[heading.end():].strip()

            if not line:
                continue

        codes = list(re.finditer(code_pattern, line))
        short_codes = re.findall(r"\b[ЗУВ3]-\d+\b", line)

        if not codes and not short_codes:
            buffer.append(line)
            continue

        flush()

        match = (
            prefix_pattern.fullmatch(line)
            or suffix_pattern.fullmatch(line)
        )

        if len(codes) != 1 or len(short_codes) != 1 or match is None:
            add_outcome(
                raw_line.strip(),
                None,
                message=(
                    "Неоднозначная запись результата обучения; "
                    "исходный текст сохранен"
                ),
            )
            continue

        kind = code_kinds.get(match.group("kind"))
        body_heading = heading_pattern.match(match.group("text"))

        if kind is None:
            kind = (
                kinds[body_heading.group(1).casefold()]
                if body_heading
                else current_kind
            )

        message = None

        if current_kind is not None and current_kind != kind:
            kind = None
            message = (
                "Вид результата в заголовке и коде различается; "
                "нужна проверка"
            )

        add_outcome(
            match.group("text"),
            kind,
            match.group("code"),
            message,
        )

    flush()

    if not outcomes and not issues:
        issues.append(ParseIssue(
            category="empty_cell",
            message="В ячейке нет непустых результатов обучения",
            source_ref=source_ref,
        ))

    return outcomes, issues

def parse_workload_number(raw):
    """Прочитать число без округления."""
    text = raw.strip().replace("\u00a0", " ").replace("\u202f", " ")

    if not re.fullmatch(
        r"(?:\d+|\d{1,3}(?: \d{3})+)(?:[.,]\d+)?", text,
    ):
        raise ValueError(f"Неподдерживаемое число: {raw!r}")

    return Decimal(text.replace(" ", "").replace(",", "."))


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
            parser_config="rpd_discipline_v1",
        )
    )

    output.parse_issues.append(
        ParseIssue(
            category="manual_review",
            severity="info",
            message=(
                "Частичный разбор: "
                "извлекаются только компетенции и индикаторы и результаты обучения."
            ),
            source_ref=SourceRef(file=file_path.name),
        )
    )

    # 3. Ищем раздел по заголовку, независимо от номера.
    discipline, discipline_issues = build_discipline(
        document,
        file_path.name,
        title_parts,
    )
    output.parse_issues.extend(discipline_issues)

    if discipline is not None:
        output.disciplines.append(discipline)

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
        outcome_column = None
        outcomes_by_cell = {}
        seen_outcome_relations = set()

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
                outcome_columns = [
                    index
                    for index, header in enumerate(headers)
                    if "результат" in header and "обучен" in header
                ]

                outcome_column = (
                    outcome_columns[0]
                    if len(outcome_columns) == 1
                    else None
                )

                if outcome_column is None:
                    output.parse_issues.append(ParseIssue(
                        category=(
                            "not_found"
                            if not outcome_columns
                            else "ambiguous"
                        ),
                        message=(
                            "Не удалось однозначно найти "
                            "колонку результатов обучения"
                        ),
                        source_ref=SourceRef(
                            file=file_path.name,
                            section=section,
                            table=table_number,
                            row=row_number,
                        ),
                    ))
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
            row_outcomes = []

            if outcome_column is not None:
                outcome_ref = SourceRef(
                    file=file_path.name,
                    section=section,
                    table=table_number,
                    row=row_number,
                    col=outcome_column + 1,
                )

                if outcome_column >= len(row.cells):
                    output.parse_issues.append(ParseIssue(
                        category="unexpected_format",
                        message=(
                            "В строке отсутствует "
                            "ячейка результатов обучения"
                        ),
                        source_ref=outcome_ref,
                    ))
                else:
                    outcome_cell = row.cells[outcome_column]

                    if outcome_cell._tc not in outcomes_by_cell:
                        values, issues = extract_learning_outcomes(
                            outcome_cell.text,
                            outcome_ref,
                            start_index=len(output.learning_outcomes) + 1,
                        )

                        outcomes_by_cell[outcome_cell._tc] = values
                        output.learning_outcomes.extend(values)
                        output.parse_issues.extend(issues)

                    row_outcomes = outcomes_by_cell[outcome_cell._tc]

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
            for outcome in row_outcomes:
                if not row_codes:
                    continue

                parents = row_codes

                if len(parsed_items) > 1:
                    parents = [
                        code
                        for code in row_codes
                        if outcome.code
                        and outcome.code.startswith(code + ".")
                    ]

                if len(parents) != 1:
                    output.parse_issues.append(ParseIssue(
                        category="ambiguous",
                        message=(
                            "Не удалось определить компетенцию "
                            f"результата {outcome.local_id}"
                        ),
                        source_ref=outcome.source_ref,
                    ))
                    continue

                relation_key = (parents[0], outcome.local_id)

                if relation_key in seen_outcome_relations:
                    continue

                seen_outcome_relations.add(relation_key)

                output.relations.append(Relation(
                    src_entity="competency",
                    src_key=parents[0],
                    dst_entity="learning_outcome",
                    dst_key=outcome.local_id,
                    kind="включает",
                    source_ref=outcome.source_ref,
                ))

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
    if output.disciplines:
        discipline = output.disciplines[0]

        for competency in output.competencies:
            output.relations.append(Relation(
                src_entity="discipline",
                src_key=discipline.name,
                dst_entity="competency",
                dst_key=competency.code,
                kind="формирует",
                source_ref=competency.source_ref,
            ))

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
        f"Дисциплин: {len(result.disciplines)}. "
        f"Компетенций: {len(result.competencies)}. "
        f"Индикаторов: {len(result.indicators)}. "
        f"Результатов обучения: {len(result.learning_outcomes)}. "
    )

if __name__ == "__main__":
    main()