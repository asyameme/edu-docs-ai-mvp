"""Преобразование подтверждённого снимка во вход существующего генератора."""

import json
import re
from dataclasses import dataclass, replace

from backend.contracts.document_v2.graph import EducationalGraph, GraphEdge, GraphNode
from backend.contracts.document_v2.models import SourceRefV2

from app.document_reconciliation import apply_document_mutations
from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_contract import DOCUMENT_MUTATION_OPS
from worker.generation import (
    CONFLICT,
    FieldChange,
    RowRemoval,
)
from worker.generation.docx_patch import (
    DocxPatch,
    DocxPatchError,
    cell_has_vertical_merge,
    patch_docx_bytes,
    read_area_text,
    read_row_cells,
)
from worker.generation.generator import SourceDocx
from worker.generation.patches import RowInsertion, with_document_changes
from worker.parser.common import normalize_competency_code
from worker.parser.results import extract_indicators
from worker.validation.validator import competency_links_for_row


class GenerationBridgeError(ValueError):
    """Сохранённую версию нельзя без потерь передать генератору."""


def snapshot_changes(snapshot: SnapshotV2) -> list[FieldChange]:
    """Собирает отличия выбранного графа от первоначальных наблюдений."""

    targets = {("entity", str(item.id)): item.attributes for item in snapshot.entities} | {
        ("relation", str(item.id)): item.attributes for item in snapshot.relations
    }
    changes = {}
    for link in snapshot.source_links:
        if (
            not link.field_path.startswith("/attributes/")
            or link.observed_value.get("calculated")
            or link.observed_value.get("workload_mirror")
        ):
            continue
        target = targets.get((link.target_kind, str(link.target_id)))
        if target is None:
            raise GenerationBridgeError("Свидетельство ссылается на отсутствующее поле графа")
        parts = link.field_path.split("/")[2:]
        value = target
        for part in parts:
            token = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict) and token in value:
                value = value[token]
            elif isinstance(value, list) and token.isdecimal() and int(token) < len(value):
                value = value[int(token)]
            else:
                raise GenerationBridgeError("Поле графа больше не соответствует источнику")
        observed = link.observed_value.get("value")
        if type(observed) is type(value) and observed == value:
            continue
        if (
            isinstance(observed, (int, float))
            and not isinstance(observed, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and observed == value
        ):
            continue
        identity = (link.target_kind, str(link.target_id), link.field_path)
        previous = changes.get(identity)
        if previous is not None and previous.new_value != value:
            raise GenerationBridgeError("Итоговые значения одного поля противоречат источникам")
        changes[identity] = FieldChange(
            target_kind=link.target_kind,
            target_id=str(link.target_id),
            field_path=link.field_path,
            new_value=value,
        )
    return list(changes.values())


_DOCUMENT_OPS = DOCUMENT_MUTATION_OPS


def _require_supported(snapshot: SnapshotV2) -> None:
    if snapshot.review is not None and snapshot.review.confirmation is None:
        raise GenerationBridgeError("Изменения требуют подтверждённой версии программы")
    for decision in snapshot.review.decisions if snapshot.review else []:
        for change in decision.changes:
            if change.get("op") not in _DOCUMENT_OPS | {"set_attribute"}:
                raise GenerationBridgeError(
                    "Генератор DOCX пока не поддерживает структурные изменения графа"
                )


def approved_changes(snapshot: SnapshotV2) -> list[FieldChange]:
    """Attribute edits only, for callers that map text replacements themselves."""
    _require_supported(snapshot)
    return snapshot_changes(snapshot)


@dataclass(frozen=True, slots=True)
class CompetencyListEdit:
    """Итог списка компетенций одной строки документа после решений пользователя.

    ``original`` and ``final`` keep the document order of the codes; ``removed``
    and ``added`` are derived from them.
    """

    document_id: str
    collection: str
    record_index: int
    record_name: str
    original: tuple[str, ...]
    final: tuple[str, ...]
    removed_rows: tuple[str, ...] | None = None
    new_links: tuple[str, ...] | None = None

    @property
    def is_plan(self) -> bool:
        return self.collection == "curriculum_items"

    @property
    def removed(self) -> tuple[str, ...]:
        if self.removed_rows is not None:
            return self.removed_rows
        return tuple(code for code in self.original if code not in self.final)

    @property
    def added(self) -> tuple[str, ...]:
        if self.new_links is not None:
            return self.new_links
        return tuple(code for code in self.final if code not in self.original)


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    """Что подтверждено и что из этого меняет исходные документы."""

    changes: list[FieldChange]
    competency_lists: list[CompetencyListEdit]


@dataclass(frozen=True, slots=True)
class SharedIndicatorEdit:
    document_id: str
    source_ref: dict
    original_text: str
    codes_and_texts: tuple[tuple[str, str], ...]
    replaced_links: frozenset[tuple[str, str]]


def _indicator_anchor(document_id, ref):
    return (document_id, *(ref.get(key) for key in ("table", "row", "col", "paragraph_index")))


def shared_indicator_edits(snapshot: SnapshotV2, parsed_documents):
    split = [
        change
        for decision in (snapshot.review.decisions if snapshot.review else [])
        for change in decision.changes
        if change.get("op") == "set_shared_indicator_text" and change["action"] == "split"
    ]
    if not split:
        return []
    refs = {str(ref.id): (str(ref.document_id), json.loads(ref.locator)) for ref in snapshot.source_refs}
    index = {}
    for entity in snapshot.entities:
        if entity.kind != "indicator":
            continue
        for ref_id in entity.source_ref_ids:
            document_id, ref = refs[str(ref_id)]
            key = (_indicator_anchor(document_id, ref), entity.attributes.get("code"))
            index.setdefault(key, []).append((entity, str(ref_id)))
    edits = []
    for choice in split:
        identity = str(choice["document_id"])
        original = [parsed_documents[identity]["indicators"][i] for i in choice["indicator_indices"]]
        ref = original[0]["source_ref"]
        codes_and_texts, replaced_links = [], set()
        for row in original:
            matches = index.get((_indicator_anchor(identity, ref), row["code"]), ())
            if len(matches) != 1:
                raise GenerationBridgeError(f"Не найден однозначный индикатор {row['code']} для общего текста")
            entity, ref_id = matches[0]
            codes_and_texts.append((row["code"], entity.attributes["text"]))
            replaced_links.add((str(entity.id), ref_id))
        edits.append(SharedIndicatorEdit(
            identity, ref, original[0]["text"], tuple(codes_and_texts), frozenset(replaced_links)
        ))
    return edits


def apply_shared_indicator_edits(documents, edits, notes):
    sources = {item.document_id: item for item in documents}
    for edit in edits:
        source = sources[edit.document_id]
        codes = [code for code, _ in edit.codes_and_texts]
        expected = "; ".join(codes) + ": " + edit.original_text
        pattern = re.compile(
            r"\s*;\s*".join(map(re.escape, codes))
            + r"\s*:\s*" + r"\s+".join(map(re.escape, edit.original_text.split()))
        )
        new = " ".join(f"{code}: {' '.join(text.split())}" for code, text in edit.codes_and_texts)
        try:
            originals = [p for p in read_area_text(source.content, edit.source_ref) if pattern.fullmatch(p.strip())]
            if len(originals) != 1:
                raise DocxPatchError(f"не найден общий фрагмент «{expected}»")
            indicators, issues = extract_indicators(
                new, codes[0].split(".", 1)[0], SourceRefV2.model_validate(edit.source_ref)
            )
            if issues or [(x.code, x.text) for x in indicators] != [
                (code, " ".join(text.split())) for code, text in edit.codes_and_texts
            ]:
                raise DocxPatchError("выбранные формулировки нельзя безопасно разделить по кодам")
            patch = DocxPatch(edit.source_ref, originals[0], new)
            patch_docx_bytes(source.content, [patch])
        except (DocxPatchError, ValueError) as error:
            notes.append(f"{source.file_name}: индикаторы {', '.join(codes)} не разделены ({error})")
            continue
        documents = with_document_changes(documents, edit.document_id, patches=[patch])
    return documents


def competency_list_edits(snapshot: SnapshotV2, original_documents) -> list[CompetencyListEdit]:
    """Списки компетенций после решений R-01 — той же логикой, что собирает граф.

    Решения воспроизводятся на неизменённом импорте (как при сохранении ответа
    и при сборке графа), поэтому итог совпадает со связями подтверждённой версии.
    """
    changes = [
        change
        for decision in (snapshot.review.decisions if snapshot.review else [])
        for change in decision.changes
        if change.get("op") in _DOCUMENT_OPS
    ]
    touched = {
        (str(change["document_id"]), change["collection"], int(change["record_index"])): change
        for change in changes
        if change.get("op") == "set_competency_links"
        and set(change["codes"]) != set(change["expected_codes"])
    }
    if not touched:
        return []
    originals = {str(key): value for key, value in original_documents.items()}
    if any(document_id not in originals for document_id, _, _ in touched):
        raise GenerationBridgeError("Документы с решениями о компетенциях отсутствуют в пакете")
    origins = {}
    try:
        selected = apply_document_mutations(originals, changes, relation_origins=origins)
    except DomainError as error:
        raise GenerationBridgeError(
            f"Решения о компетенциях не воспроизводятся на исходных документах: {error.detail}"
        ) from error
    edits = []
    for document_id, collection, index in sorted(touched):
        row = originals[document_id][collection][index]
        before = competency_links_for_row(originals[document_id], collection, index)
        after = competency_links_for_row(selected[document_id], collection, index)
        provenance = origins.get(document_id)
        removed, added = None, None
        if collection == "disciplines" and provenance is not None:
            retained = {provenance[position] for position, _ in after}
            removed = tuple(dict.fromkeys(
                relation["dst_key"] for position, relation in before if position not in retained
            ))
            added = tuple(dict.fromkeys(
                relation["dst_key"] for position, relation in after if provenance[position] is None
            ))
        edits.append(
            CompetencyListEdit(
                document_id=document_id,
                collection=collection,
                record_index=index,
                record_name=str(row.get("discipline_name") or row.get("name") or ""),
                original=tuple(dict.fromkeys(relation["dst_key"] for _, relation in before)),
                final=tuple(dict.fromkeys(relation["dst_key"] for _, relation in after)),
                removed_rows=removed,
                new_links=added,
            )
        )
    return [edit for edit in edits if edit.removed or edit.added]


def snapshot_plan(snapshot: SnapshotV2, original_documents) -> GenerationPlan:
    """Подтверждённые правки атрибутов и решения о списках компетенций."""
    _require_supported(snapshot)
    return GenerationPlan(
        changes=snapshot_changes(snapshot),
        competency_lists=competency_list_edits(snapshot, original_documents),
    )


def _listed_codes(text: str) -> set[str]:
    return {part for part in re.split(r"[,;\s]+", text.strip()) if part}


def _row_coordinates(ref) -> tuple[int, int] | None:
    if (
        isinstance(ref, dict)
        and isinstance(ref.get("table"), int)
        and isinstance(ref.get("row"), int)
    ):
        return ref["table"], ref["row"]
    return None


def _row_ref(coordinates) -> dict[str, int]:
    return dict(zip(("table", "row"), coordinates, strict=True))


def _describe_change(edit: CompetencyListEdit) -> str:
    parts = []
    if edit.removed:
        parts.append("убрать: " + ", ".join(edit.removed))
    if edit.added:
        parts.append("добавить: " + ", ".join(edit.added))
    return "; ".join(parts)


def _edit_plan_codes(documents, source, edit, table, row, notes, where):
    """Учебный план: список компетенций строки — это текст одной ячейки."""
    try:
        cells = read_row_cells(source.content, table, row)
    except DocxPatchError as error:
        notes.append(f"{where}: строка учебного плана не прочитана ({error})")
        return documents
    matches = [
        (column, text)
        for column, text in enumerate(cells, 1)
        if _listed_codes(text) == set(edit.original)
    ]
    if len(matches) != 1:
        notes.append(
            f"{where}: ячейка со списком компетенций не найдена однозначно — "
            f"измените список вручную ({_describe_change(edit)})"
        )
        return documents
    column, text = matches[0]
    separator = ", " if ", " in text else "," if "," in text else ", "
    patch = DocxPatch(
        source_ref={"table": table, "row": row, "col": column},
        old_text=text,
        new_text=separator.join(edit.final),
    )
    try:
        if cell_has_vertical_merge(source.content, table, row, column):
            raise DocxPatchError("ячейка со списком общая для нескольких строк плана")
        patch_docx_bytes(source.content, [patch])
    except DocxPatchError as error:
        notes.append(
            f"{where}: список компетенций не заменён автоматически ({error}) — "
            f"измените вручную ({_describe_change(edit)})"
        )
        return documents
    return with_document_changes(documents, source.document_id, patches=[patch])


_KIND_HEADERS = {"знать": "знает:", "уметь": "умеет:", "владеть": "владеет:"}


@dataclass(frozen=True, slots=True)
class CompetencyRowContent:
    """Содержание новой строки компетенции, собранное по источникам в графе.

    ``source_label`` называет документ-источник набора; ``complete`` — False,
    когда результаты обучения для этой дисциплины нигде не записаны и их
    составляет человек (индикаторы при этом переносятся).
    """

    formulation: str
    indicators: tuple[dict, ...]
    outcomes: tuple[dict, ...]
    tools: tuple[str, ...]
    source_label: str
    complete: bool


def _doc_records(output, key, code):
    return [
        item for item in output.get(key, [])
        if str(item.get("code") or "") == code
        or str(item.get("competency_code") or "") == code
    ]


def _doc_label(output):
    return (output.get("meta") or {}).get("file_name") or "?"


def _ordered_unique(records, key_of, wanted):
    seen, result = set(), []
    for record in records:
        key = key_of(record)
        if key in wanted and key not in seen:
            seen.add(key)
            result.append(record)
    return result


def _formulation_of(output, code):
    for item in output.get("competencies", []):
        if str(item.get("code") or "") == code and item.get("formulation"):
            return str(item["formulation"]).strip()
    return ""


def _set_from_sibling(parsed_documents, target_output, target_id, code):
    """Набор из другого документа той же дисциплины (пара ТОП/Стандарт на слоте)."""
    names = {d.get("name") for d in target_output.get("disciplines", [])}
    for document_id, output in parsed_documents.items():
        if document_id == target_id:
            continue
        if (output.get("meta") or {}).get("doc_type") not in {"RPD", "RPP"}:
            continue
        if not names & {d.get("name") for d in output.get("disciplines", [])}:
            continue
        if not _doc_records(output, "competencies", code):
            continue
        indicator_codes = {
            str(r.get("dst_key")) for r in output.get("relations", [])
            if r.get("kind") == "имеет" and str(r.get("src_key")) == code
        }
        indicators = _ordered_unique(
            output.get("indicators", []), lambda i: str(i.get("code") or ""), indicator_codes
        )
        outcome_ids = {
            str(r.get("dst_key")) for r in output.get("relations", [])
            if r.get("kind") == "включает" and str(r.get("src_key")) == code
        }
        outcomes = _ordered_unique(
            output.get("learning_outcomes", []), lambda o: str(o.get("local_id") or ""),
            outcome_ids,
        )
        tools = tuple(dict.fromkeys(
            str(r.get("dst_key")) for r in output.get("relations", [])
            if r.get("kind") == "проверяется" and str(r.get("src_key")) in indicator_codes
        ))
        return CompetencyRowContent(
            formulation=_formulation_of(output, code),
            indicators=tuple(indicators), outcomes=tuple(outcomes), tools=tools,
            source_label=f"одноимённой дисциплины в «{_doc_label(output)}»",
            complete=True,
        )
    return None


def _set_from_ckmv(parsed_documents, code):
    """Канонический набор из картотеки ЦКМВ."""
    for output in parsed_documents.values():
        if (output.get("meta") or {}).get("doc_type") != "CKMV":
            continue
        indicators = [
            item for item in output.get("indicators", [])
            if str(item.get("competency_code") or "") == code and item.get("text")
        ]
        if not indicators:
            continue
        codes = {str(item.get("code")) for item in indicators}
        outcome_ids = {
            str(link.get("learning_outcome_id"))
            for link in output.get("indicator_outcome_links", [])
            if str(link.get("indicator_code")) in codes
        }
        outcomes = _ordered_unique(
            output.get("learning_outcomes", []), lambda o: str(o.get("local_id") or ""),
            outcome_ids,
        )
        return CompetencyRowContent(
            formulation=_formulation_of(output, code),
            indicators=tuple(indicators), outcomes=tuple(outcomes), tools=(),
            source_label="картотеки ЦКМВ", complete=True,
        )
    return None


def _set_from_program(parsed_documents, code):
    """Индикаторы из всех документов программы; результаты — не выдумываем."""
    texts: dict[str, dict[str, int]] = {}
    for output in parsed_documents.values():
        if (output.get("meta") or {}).get("doc_type") not in {"RPD", "RPP", "CKMV"}:
            continue
        for item in output.get("indicators", []):
            if str(item.get("competency_code") or "") != code or not item.get("text"):
                continue
            entry = texts.setdefault(str(item.get("code")), {})
            text = str(item.get("text"))
            entry[text] = entry.get(text, 0) + 1
    if not texts:
        return None
    indicators = tuple(
        {"code": indicator_code, "text": max(text_counts, key=text_counts.get)}
        for indicator_code, text_counts in sorted(texts.items())
    )
    return CompetencyRowContent(
        formulation="", indicators=indicators, outcomes=(), tools=(),
        source_label="индикаторов программы (объединение по документам)",
        complete=False,
    )


def _competency_formulation(parsed_documents, code):
    """Формулировка по приоритету источников; после R-07 она в документах едина."""
    priority = ("CKMV", "CHARACTERISTIC", "CURRICULUM", "RPD", "RPP")
    by_type: dict[str, list] = {}
    for output in parsed_documents.values():
        by_type.setdefault((output.get("meta") or {}).get("doc_type") or "?", []).append(output)
    for doc_type in priority:
        for output in by_type.get(doc_type, []):
            formulation = _formulation_of(output, code)
            if formulation:
                return formulation
    return ""


def _competency_row_content(parsed_documents, target_output, target_id, code):
    """Набор для новой строки: сиблинг дисциплины → ЦКМВ → объединение программы."""
    sibling = _set_from_sibling(parsed_documents, target_output, target_id, code)
    if sibling:
        return sibling
    ckmv = _set_from_ckmv(parsed_documents, code)
    if ckmv:
        return ckmv
    fallback = _set_from_program(parsed_documents, code)
    if fallback is not None:
        return replace(
            fallback,
            formulation=(
                _competency_formulation(parsed_documents, code)
                or _formulation_of(target_output, code)
            ),
        )
    return None


def _outcome_lines(outcomes):
    if not outcomes:
        return ("знает:", "умеет:")
    grouped: dict[str, list] = {}
    for outcome in outcomes:
        kind = str(outcome.get("kind") or "знать").strip().casefold()
        grouped.setdefault(kind, []).append(outcome)
    lines = []
    for kind, group in grouped.items():
        lines.append(_KIND_HEADERS.get(kind, f"{kind}:"))
        for outcome in group:
            code = str(outcome.get("code") or "").strip()
            text = str(outcome.get("text") or "").strip()
            if code and text.startswith(code):
                body = normalize_competency_code(code) + text[len(code):]
            elif code and text:
                body = f"{normalize_competency_code(code)}. {text}"
            else:
                body = text or code
            lines.append(f"• {body}")
    return tuple(lines)


def _results_table_of(output):
    """Таблица результатов и строка-шаблон по координатам компетенций."""
    locations = [
        _row_coordinates(item.get("source_ref"))
        for item in output.get("competencies", [])
    ]
    return _single_table(locations)


def _assessment_table_of(output):
    """Таблица КИМ и строка-шаблон по координатам связей «проверяется»."""
    locations = [
        _row_coordinates(relation.get("source_ref"))
        for relation in output.get("relations", [])
        if relation.get("kind") == "проверяется"
    ]
    return _single_table(locations)


def _single_table(locations):
    locations = [location for location in locations if location]
    if not locations:
        return None
    tables = {table for table, _ in locations}
    if len(tables) != 1:
        return None
    return tables.pop(), max(row for _, row in locations)


def _edit_discipline_rows(documents, source, output, edit, notes, where, parsed_documents):
    """РПД: компетенция — строка таблицы результатов и строки оценочных средств."""
    def code_at(record, key):
        return str(record.get(key) or ""), _row_coordinates(record.get("source_ref"))

    competencies = [code_at(item, "code") for item in output.get("competencies", [])]
    assessments = [
        code_at(item, "src_key") for item in output.get("relations", [])
        if item.get("kind") == "проверяется" and item.get("src_entity") == "indicator"
    ]
    indicators = [code_at(item, "code") for item in output.get("indicators", [])]
    formations = [
        code_at(item, "dst_key") for item in output.get("relations", [])
        if (item.get("kind"), item.get("src_entity"), item.get("dst_entity"))
        == ("формирует", "discipline", "competency")
    ]
    removed = set(edit.removed)
    manual = removed - {code for code, location in competencies if location}
    for code in sorted(manual):
        notes.append(
            f"{where}: не найдена отдельная строка компетенции {code} — проверьте вручную"
        )

    def is_removed(code):
        return any(code == prefix or code.startswith(prefix + ".") for prefix in removed)

    shared = {
        location for code, location in [*competencies, *assessments, *indicators, *formations]
        if code and location and not is_removed(code)
    }
    removals = {}
    for code, location in [*competencies, *assessments]:
        if not code or not is_removed(code):
            continue
        if location in shared:
            table, row = location
            notes.append(
                f"{where}: таблица {table}, строка {row} содержит «{code}» и другие "
                "компетенции или индикаторы — измените строку вручную"
            )
        elif location:
            removals.setdefault(location, RowRemoval(_row_ref(location), code))
        elif code not in manual:
            notes.append(f"{where}: нет координат строки «{code}» — проверьте вручную")
    if removals:
        documents = with_document_changes(
            documents, source.document_id, row_removals=removals.values()
        )

    # Новая компетенция: набор берётся из графа по источникам (сиблинг
    # дисциплины → картотека ЦКМВ → объединение программы), а не составляется.
    insertions: list[RowInsertion] = []
    results_table = _results_table_of(output)
    assessment_table = _assessment_table_of(output)
    target_tools = tuple(dict.fromkeys(
        str(tool.get("name")) for tool in output.get("assessment_tools", []) if tool.get("name")
    ))
    for code in edit.added:
        content = _competency_row_content(parsed_documents, output, edit.document_id, code)
        if content is None or not (content.indicators or content.outcomes):
            notes.append(
                f"{where}: добавьте компетенцию {code} вручную — в программе нет её "
                "индикаторов и результатов"
            )
            continue
        if results_table is None:
            notes.append(
                f"{where}: нет координат таблицы результатов — компетенцию {code} "
                "добавьте вручную"
            )
            continue
        formulation = content.formulation or _competency_formulation(parsed_documents, code)
        table, template_row = results_table
        insertions.append(RowInsertion(
            {"table": table, "row": template_row},
            (
                (f"{code}. {formulation}".rstrip(". "),),
                _outcome_lines(content.outcomes),
                tuple(f"{item['code']}. {item['text']}" for item in content.indicators),
            ),
        ))
        tools = content.tools or target_tools
        if assessment_table is not None and content.indicators:
            kim_table, kim_template = assessment_table
            for item in content.indicators:
                insertions.append(RowInsertion(
                    {"table": kim_table, "row": kim_template},
                    (("0",), (f"{item['code']}. {item['text']}",), tools or ("",)),
                ))
        if content.complete:
            notes.append(
                f"{where}: добавлена компетенция {code} — строка результатов и "
                f"{len(content.indicators)} строк КИМ составлены из {content.source_label}; "
                "проверьте содержание"
            )
        else:
            notes.append(
                f"{where}: добавлена компетенция {code} — индикаторы перенесены из "
                f"{content.source_label}; результаты обучения (знает/умеет) составьте "
                "для этой дисциплины"
            )
    if insertions:
        documents = with_document_changes(
            documents, source.document_id, row_insertions=insertions
        )
    return documents


def apply_competency_list_edits(documents, edits, parsed_documents, notes) -> list[SourceDocx]:
    """Переносит решения о списках компетенций в документы.

    Что перенести нельзя, попадает в ``notes`` для отчёта генерации, а не
    останавливает сборку всего пакета.
    """
    sources = {document.document_id: document for document in documents}
    result = list(documents)
    for edit in edits:
        source, output = sources.get(edit.document_id), parsed_documents.get(edit.document_id)
        if source is None or output is None:
            raise GenerationBridgeError("Документ с решением о компетенциях отсутствует в пакете")
        where = f"{source.file_name} ({edit.record_name})"
        try:
            row = output[edit.collection][edit.record_index]
        except (KeyError, IndexError, TypeError) as error:
            raise GenerationBridgeError(
                f"Строка с решением о компетенциях не найдена в разборе {source.file_name}"
            ) from error
        coordinates = _row_coordinates(row.get("source_ref"))
        if coordinates is None:
            notes.append(
                f"{where}: нет координат строки в документе — измените список "
                f"компетенций вручную ({_describe_change(edit)})"
            )
            continue
        table, row_number = coordinates
        if edit.is_plan:
            result = _edit_plan_codes(result, source, edit, table, row_number, notes, where)
        else:
            result = _edit_discipline_rows(
                result, source, output, edit, notes, where, parsed_documents
            )
    return result


_FIELD_LABELS = {
    "hours_total": "общий объём часов",
    "credit_units": "зачётные единицы",
    "hours_by_kind": "часы по видам нагрузки",
    "formulation": "формулировка",
    "code": "код",
    "name": "название",
    "text": "текст",
    "semester": "семестр",
}


_FIELD_LABELS = {
    "hours_total": "общий объём часов",
    "credit_units": "зачётные единицы",
    "hours_by_kind": "часы по видам нагрузки",
    "formulation": "формулировка",
    "code": "код",
    "name": "название",
    "text": "текст",
    "semester": "семестр",
}


def _shorten(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def field_label(field_path: str) -> str:
    """Имя атрибута на обычном языке; неизвестное остаётся как есть."""
    tail = field_path.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    return _FIELD_LABELS.get(tail, tail)


def describe_unresolved(snapshot: SnapshotV2, documents, unresolved) -> list[str]:
    """Правки, которые не перенеслись в документы, — словами для отчёта генерации."""
    names = {
        ("entity", str(item.id)): item.attributes.get("name") or item.attributes.get("code")
        for item in snapshot.entities
    }
    files = {document.document_id: document.file_name for document in documents}
    notes = []
    for item in unresolved:
        change = item.patch.change
        who = names.get((change.target_kind, change.target_id))
        where = f" ({who})" if isinstance(who, str) and who else ""
        file = files.get(item.document_id, item.document_id)
        label = field_label(change.field_path)
        old, new = (_shorten(item.patch.old_text), _shorten(item.patch.new_text))
        if item.reason:
            notes.append(
                f"{file}{where}: значение «{new}» поля «{label}» не перенесено "
                f"({item.reason}) — исправьте вручную"
            )
        elif item.occurrences == CONFLICT:
            notes.append(
                f"{file}{where}: за текст «{old}» поля «{label}» отвечают разные решения; "
                f"оставлен прежний текст, значение «{new}» не перенесено — выберите одно значение"
            )
        elif item.occurrences == 0:
            notes.append(
                f"{file}{where}: прежнее значение «{old}» поля «{label}» не найдено "
                f"в документе; новое значение «{new}» не перенесено — исправьте вручную"
            )
        else:
            notes.append(
                f"{file}{where}: прежнее значение «{old}» поля «{label}» встречается "
                f"в источнике несколько раз; новое значение «{new}» не перенесено — "
                "исправьте вручную"
            )
    return notes


def _source_ref(snapshot: SnapshotV2, ref_id):
    ref = next((item for item in snapshot.source_refs if item.id == ref_id), None)
    if ref is None:
        raise GenerationBridgeError("Узел графа ссылается на отсутствующий источник")
    try:
        locator = json.loads(ref.locator)
    except (ValueError, TypeError):
        locator = {}
    if not isinstance(locator, dict):
        locator = {}
    locator = {key: locator[key] for key in SourceRefV2.model_fields if key in locator}
    locator.setdefault("file", str(ref.document_id))
    locator.setdefault("section", ref.section)
    return SourceRefV2.model_validate(locator)


def snapshot_matrix_graph(snapshot: SnapshotV2) -> EducationalGraph:
    """Матрица строится из выбранной версии, а не из прежнего вывода парсера."""
    graph = EducationalGraph()
    program = str(snapshot.metadata.get("program_id") or "program")
    node_ids = {}
    for entity in snapshot.entities:
        attributes = entity.attributes
        if entity.kind == "discipline":
            key = attributes.get("name")
        elif entity.kind == "competency":
            key = attributes.get("code")
        elif entity.kind == "curriculum_item":
            name, semester = attributes.get("discipline_name"), attributes.get("semester")
            key = f"{name}::semester={semester}" if name and semester is not None else None
        else:
            continue
        if not isinstance(key, str) or not key.strip():
            raise GenerationBridgeError(f"У объекта {entity.id} нет ключа для матрицы")
        identity = f"{program}|{entity.kind}|{key}"
        node_ids[entity.id] = identity
        graph.add_node(
            GraphNode(
                id=identity,
                entity_type=entity.kind,
                attributes=attributes,
                source_refs=[_source_ref(snapshot, ref_id) for ref_id in entity.source_ref_ids],
            )
        )
    for relation in snapshot.relations:
        if relation.type != "FORMS_COMPETENCY" or relation.attributes.get("matrix_value") == 0:
            continue
        source = node_ids.get(relation.source_id)
        target = node_ids.get(relation.target_id)
        if source is None or target is None:
            raise GenerationBridgeError("Связь матрицы указывает на неподдерживаемый объект")
        if not relation.source_ref_ids:
            raise GenerationBridgeError("У связи матрицы нет источника")
        graph.add_edge(
            GraphEdge(
                source_id=source,
                target_id=target,
                relation_type="формирует",
                source_ref=_source_ref(snapshot, relation.source_ref_ids[0]),
                document_type="SNAPSHOT",
                semester=relation.attributes.get("semester"),
                value=relation.attributes.get("matrix_value"),
            )
        )
    return graph
