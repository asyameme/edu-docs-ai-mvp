"""Разбор компетенций, индикаторов, результатов обучения и их связей."""

import re
from pathlib import Path

from docx.table import Table
from docx.text.paragraph import Paragraph

from .common import (
    heading_level,
    is_subsection,
    normalize,
    normalize_universal_code,
    read_section,
)
from backend.contracts.document_v2.models import (
    Competency,
    Indicator,
    LearningOutcome,
    ParseIssue,
    Relation,
    SourceRef,
)


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
        return normalize_universal_code(match.group(1)), match.group(2) or ""

    match = re.fullmatch(
        rf"(.+?)\s*\(({code_pattern})\)\.?",
        text,
    )

    if match:
        return normalize_universal_code(match.group(2)), match.group(1).strip()

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


def _trim_segment(text):
    """Убрать разделители между сегментами мульти-результатной строки."""
    return re.sub(
        r"^[.:\s;,–—-]+|[.\s;,]+$",
        "",
        " ".join(text.split()),
    ).strip()


def _multi_result_segments(line, codes):
    """Разбить строку из нескольких результатов на пары (коды, текст).

    Поддержаны два оформления: код в начале сегмента («КОД — текст.
    КОД — текст.», включая группы «КОД1, КОД2 — текст» и короткие коды
    «У-2 — текст» с префиксом от предыдущего полного кода) и код
    в скобках в конце сегмента («текст (КОД); текст (КОД).»). Если
    разбиение неоднозначно, возвращается пустой список.
    """
    if codes[0].start() == 0:
        tokens = [
            (match.start(), match.end(), "full", match)
            for match in codes
        ]

        for match in re.finditer(r"\b[ЗУВ3]-\d+\b", line):
            if any(start <= match.start() < end for start, end, _, _ in tokens):
                continue
            if re.fullmatch(r"[,\s]*", line[:match.start()]) is not None:
                continue
            tokens.append((match.start(), match.end(), "short", match))

        tokens.sort()
        segments = []
        index = 0

        while index < len(tokens):
            group = [tokens[index]]
            end = tokens[index][1]
            index += 1

            while index < len(tokens):
                between = line[end:tokens[index][0]]

                if not re.fullmatch(r"[,\s]*", between):
                    break
                group.append(tokens[index])
                end = tokens[index][1]
                index += 1

            text_end = tokens[index][0] if index < len(tokens) else len(line)
            text = _trim_segment(line[end:text_end])

            if not text:
                return []
            segments.append((group, text))

        return segments

    if all(
        match.start() > 0
        and line[match.start() - 1] == "("
        and line[match.end():match.end() + 1] == ")"
        for match in codes
    ):
        segments = []
        cursor = 0
        for match in codes:
            text = _trim_segment(line[cursor:match.start() - 1])
            cursor = match.end() + 1
            if not text:
                return []
            segments.append((
                [(match.start(), match.end(), "full", match)],
                text,
            ))
        if not re.fullmatch(r"[;,.\s]*", line[cursor:]):
            return []
        return segments

    return []


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
        # В кодах документов цифра 3 используется вместо похожей буквы З.
        "3": "знать",
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
    outcome_code = re.compile(code_pattern)
    suffix_pattern = re.compile(r"^(?P<text>.+?)\s*\((?P<codes>[^()]+)\)[.;]?$")

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

        # В «Уметь:» задан вид группы, а в «владеть навыками ... (код У-1)»
        # глагол является частью самого результата.
        if heading and (
            not outcome_code.search(line)
            or ":" in heading.group()
        ):
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

        if len(codes) >= 2 or (codes and len(short_codes) > len(codes)):
            segments = _multi_result_segments(line, codes)

            if segments:
                prefix_code = None

                for group, text in segments:
                    for _, _, kind_token, token in group:
                        if kind_token == "full":
                            code = token.group("code")
                            kind = code_kinds[token.group("kind")]
                            prefix_code = code.rsplit(" ", 1)[0]
                        else:
                            code = f"{prefix_code} {token.group(0)}"
                            kind = code_kinds[token.group(0)[0]]

                        message = None

                        if current_kind is not None and current_kind != kind:
                            kind = None
                            message = (
                                "Вид результата в заголовке и коде "
                                "различается; нужна проверка"
                            )
                        add_outcome(text, kind, code, message)

                continue

        prefix = prefix_pattern.fullmatch(line)
        suffix = suffix_pattern.fullmatch(line)
        if prefix and len(codes) == len(short_codes) == 1:
            description = prefix.group("text")
            matches = [prefix]
        elif suffix:
            description = suffix.group("text")
            matches = [
                outcome_code.fullmatch(token.strip())
                for token in re.split(r"[,;]", suffix.group("codes"))
            ]
        else:
            matches = []

        if not matches or len(codes) != len(matches) or not all(matches):
            add_outcome(
                raw_line.strip(),
                None,
                message=(
                    "Неоднозначная запись результата обучения; "
                    "исходный текст сохранен"
                ),
            )
            continue

        for match in matches:
            kind = code_kinds[match.group("kind")]
            message = None
            if current_kind is not None and current_kind != kind:
                kind = None
                message = (
                    "Вид результата в заголовке и коде различается; "
                    "нужна проверка"
                )
            add_outcome(description, kind, match.group("code"), message)

    flush()

    if not outcomes and not issues:
        issues.append(ParseIssue(
            category="empty_cell",
            message="В ячейке нет непустых результатов обучения",
            source_ref=source_ref,
        ))

    return outcomes, issues


def extract_results_into(output, document, file_name, is_heading):
    """Добавить сущности из таблиц раздела результатов обучения."""
    blocks = list(document.iter_inner_content())
    file_path = Path(file_name)

    starts = [
        index
        for index, block in enumerate(blocks)
        if isinstance(block, Paragraph)
        and is_heading(block)
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
    root_section = section
    depth = heading_level(blocks[start]) or 2

    #считаем таблицы от начала документа
    table_number = sum(
        isinstance(block, Table)
        for block in blocks[:start]
    )

    found_table = False

    #читаем содержимое найденного раздела
    # Компетенции, упомянутые кодом без локальной формулировки: сущность не
    # создаём, но связь «формирует» нужна — иначе граф считает, что РПД не
    # формирует компетенцию, хотя строка таблицы её явно называет.
    code_only_refs: dict[str, SourceRef] = {}
    for block in blocks[start + 1:]:
        if isinstance(block, Paragraph):
            level = heading_level(block)

            if level is not None:
                if level <= depth or not is_subsection(root_section, block.text):
                    break

                section = block.text.strip()

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
                        if not known_formulations:
                            # Строка ссылается на компетенцию кодом без локальной
                            # формулировки (описание живет в других документах).
                            # Код всё равно определяет принадлежность результатов
                            # и индикаторов этой строки таблицы.
                            row_codes.append(competency_code)
                            code_only_refs.setdefault(competency_code, ref)
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

            if indicator_column is None:
                continue

            indicator_parent_codes = list(dict.fromkeys(
                parsed[0] for parsed in parsed_items if parsed is not None
            ))
            if not indicator_parent_codes:
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
                indicator_parent_codes[0],
                indicator_ref,
            )
            output.parse_issues.extend(issues)

            for indicator in indicators:
                if len(parsed_items) > 1:
                    parents = [
                        code for code in indicator_parent_codes
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
                if indicator.competency_code not in row_codes:
                    continue

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

        for code, ref in code_only_refs.items():
            output.relations.append(Relation(
                src_entity="discipline",
                src_key=discipline.name,
                dst_entity="competency",
                dst_key=code,
                kind="формирует",
                source_ref=ref,
            ))

    _dedupe_relations(output)
    return output


def _dedupe_relations(output):
    """Убрать повторные одинаковые связи дисциплина → компетенция.

    Компетенция может занимать несколько строк таблицы (по строке на индикатор);
    каждая строка порождала свою связь «формирует», хотя это одна и та же связь.
    """
    seen = set()
    kept = []
    for relation in output.relations:
        if relation.kind != "формирует" or relation.src_entity != "discipline":
            kept.append(relation)
            continue
        key = (relation.src_key, relation.dst_key)
        if key in seen:
            continue
        seen.add(key)
        kept.append(relation)
    output.relations = kept


def extract_practice_competencies(output, document, file_name, is_heading):
    """Добавить компетенции из текстовых перечней РПП."""
    blocks, issues = read_section(
        document, file_name, is_heading, "компетенции практики",
        keep_numbered_items=True,
    )
    if not blocks:
        output.parse_issues.extend(issues)
        return output

    def signature(code, formulation):
        return code, normalize(formulation).rstrip(".")

    table_mentions = {
        signature(item.code, item.formulation)
        for item in output.competencies
    }
    before_count = len(output.competencies)
    paragraph_counts = {}

    for block, ref in blocks:
        if not isinstance(block, Paragraph):
            continue
        for parsed in split_competencies(block.text):
            if parsed is None:
                if re.match(
                    r"^\s*[А-ЯA-Z][А-ЯA-Z0-9]*(?:[-\u2013\u2014][А-ЯA-Z0-9]+)+",
                    block.text,
                ):
                    output.parse_issues.append(ParseIssue(
                        category="unparsed_row",
                        message=(
                            "Не разобрана запись текстового перечня компетенций: "
                            + " ".join(block.text.split())
                        ),
                        source_ref=ref,
                    ))
                continue
            code, formulation = parsed
            if not formulation:
                output.parse_issues.append(ParseIssue(
                    category="empty_cell",
                    message=f"В текстовом перечне нет формулировки компетенции {code}",
                    source_ref=ref,
                ))
                continue

            key = signature(code, formulation)
            paragraph_counts[key] = paragraph_counts.get(key, 0) + 1
            # Первое совпадение перечня с подробной таблицей - одно
            # упоминание сущности. Реальные повторы внутри перечня сохраняем.
            if key in table_mentions and paragraph_counts[key] == 1:
                continue

            competency = Competency(
                code=code, formulation=formulation, source_ref=ref,
            )
            output.competencies.append(competency)
            for discipline in output.disciplines:
                output.relations.append(Relation(
                    src_entity="discipline",
                    src_key=discipline.name,
                    dst_entity="competency",
                    dst_key=code,
                    kind="формирует",
                    source_ref=ref,
                ))

    if not before_count and output.competencies:
        output.parse_issues = [
            issue for issue in output.parse_issues
            if not (
                issue.category == "not_found"
                and issue.message == "Не найдена таблица компетенций"
            )
        ]
        output.parse_issues.append(ParseIssue(
            category="manual_review",
            message=(
                "Компетенции прочитаны из текстового перечня; "
                "табличные соответствия индикаторам и результатам не найдены"
            ),
            source_ref=blocks[0][1],
        ))
    _dedupe_relations(output)
    return output
