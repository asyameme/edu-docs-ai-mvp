"""Тесты слоя «правки сущностей → патчи документов»: plan → resolve → генерация."""

import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from test_generation_docx_patch import document_text, make_docx

from worker.generation import (
    FieldChange,
    FieldEvidence,
    GenerationMappingError,
    OriginalDocument,
    SourceEvidence,
    build_generation_documents,
    generate_docx_package,
    patch_docx_bytes,
    plan_patches,
    read_area_text,
    resolve_planned_patches,
)

ORIGINALS = Path(__file__).resolve().parents[2] / "tests" / "originals"


def _change(field_path="/attributes/hours", value=108, target_id="e-1"):
    return FieldChange("entity", target_id, field_path, value)


def _link(field_path="/attributes/hours", ref_id="ref-1", value=72, unit="ч", target_id="e-1"):
    return FieldEvidence("entity", target_id, field_path, ref_id, {"value": value, "unit": unit})


def _ref(ref_id="ref-1", document_id="doc-1", **coordinates):
    return SourceEvidence(ref_id, document_id, json.dumps(coordinates, sort_keys=True))


def _new_text(change, link, ref=None):
    planned = plan_patches([change], [link], [ref or _ref()])
    return planned["doc-1"][0].new_text


def test_change_spreads_over_documents():
    links = [_link(ref_id="ref-1"), _link(ref_id="ref-2")]
    refs = [
        _ref("ref-1", "doc-1", table=1, row=2, col=3),
        _ref("ref-2", "doc-2", paragraph_index=7),
    ]
    planned = plan_patches([_change()], links, refs)
    assert set(planned) == {"doc-1", "doc-2"}
    assert [patch.old_text for patch in planned["doc-1"]] == ["72 ч"]
    assert planned["doc-2"][0].source_ref == {"paragraph_index": 7}


def test_change_without_evidence_fails():
    with pytest.raises(GenerationMappingError, match="нет записанных свидетельств"):
        plan_patches([_change()], links=[], refs=[])


def test_unknown_source_ref_fails():
    with pytest.raises(GenerationMappingError, match="неизвестный source_ref"):
        plan_patches([_change()], links=[_link(ref_id="ref-x")], refs=[])


def test_conflicting_changes_fail_and_identical_collapse():
    links, refs = [_link()], [_ref()]
    with pytest.raises(GenerationMappingError, match="Противоречивые изменения"):
        plan_patches([_change(value=108), _change(value=144)], links, refs)
    planned = plan_patches([_change(value=108), _change(value=108)], links, refs)
    assert len(planned["doc-1"]) == 1


def test_rendering_rules():
    assert _new_text(_change(value=72.5), _link(unit=None)) == "72.5"
    assert _new_text(_change(value=72.0), _link()) == "72 ч"
    assert _new_text(_change(value="36 з.е."), _link(unit=None)) == "36 з.е."
    for value in (None, True):
        with pytest.raises(GenerationMappingError, match="нельзя записать в документ"):
            plan_patches([_change(value=value)], [_link()], [_ref()])
    with pytest.raises(GenerationMappingError, match="одним текстовым фрагментом"):
        plan_patches([_change(value="два\nстроки")], [_link()], [_ref()])


def test_bad_observed_value_and_locator_fail():
    broken_link = FieldEvidence("entity", "e-1", "/attributes/hours", "ref-1", {"unit": "ч"})
    with pytest.raises(GenerationMappingError, match="нет наблюдаемого значения"):
        plan_patches([_change()], [broken_link], [_ref()])
    with pytest.raises(GenerationMappingError, match="не является корректным JSON"):
        plan_patches([_change()], [_link()], [SourceEvidence("ref-1", "doc-1", "{не json")])


def test_stale_observed_value_fails_before_patch():
    docx = make_docx(
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Трудоёмкость</w:t></w:r></w:p></w:tc>'
        "<w:tc><w:p><w:r><w:t>72 ч</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    ref = _ref("ref-1", "doc-1", table=1, row=1, col=2)
    stale = FieldEvidence(
        "entity", "e-1", "/attributes/hours", "ref-1", {"value": 999, "unit": "ч"}
    )
    stale_plan = plan_patches([_change(value=108)], [stale], [ref])
    with pytest.raises(GenerationMappingError, match="свидетельство устарело"):
        resolve_planned_patches({"doc-1": docx}, stale_plan)
    fresh_plan = plan_patches([_change(value=108)], [_link(ref_id="ref-1")], [ref])
    resolved = resolve_planned_patches({"doc-1": docx}, fresh_plan)
    patched_doc = patch_docx_bytes(docx, resolved["doc-1"])
    assert "108 ч" in document_text(patched_doc)
    assert "Трудоёмкость" in document_text(patched_doc)


def test_two_changes_claiming_one_fragment_fail():
    docx = make_docx(_make_paragraph("72 ч"))
    links = [
        _link(field_path="/attributes/hours", ref_id="ref-1"),
        _link(field_path="/attributes/note", value=72, unit="ч", ref_id="ref-2"),
    ]
    refs = [_ref("ref-1", "doc-1"), _ref("ref-2", "doc-1")]
    planned = plan_patches(
        [_change(value=108), _change(field_path="/attributes/note", value=109)],
        links,
        refs,
    )
    with pytest.raises(GenerationMappingError, match="претендуют на один фрагмент"):
        resolve_planned_patches({"doc-1": docx}, planned)


def _make_paragraph(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_end_to_end_package_generation():
    doc_rpd = make_docx(_make_paragraph("Трудоёмкость: 72 ч"))
    doc_plan = make_docx(_make_paragraph("Семестр 3, часов: 72 ч"))
    doc_char = make_docx(_make_paragraph("Характеристика программы"))
    originals = [
        OriginalDocument("doc-1", "RPD", "РПД.docx", doc_rpd),
        OriginalDocument("doc-2", "CURRICULUM", "Учебный план.docx", doc_plan),
        OriginalDocument("doc-3", "CHARACTERISTIC", "Характеристика.docx", doc_char),
    ]
    links = [_link(ref_id="ref-1"), _link(ref_id="ref-2")]
    refs = [
        _ref("ref-1", "doc-1", paragraph_index=1),
        _ref("ref-2", "doc-2", paragraph_index=1),
    ]
    documents = build_generation_documents([_change(value=108)], links, refs, originals)
    assert documents[2].patches == ()
    package = generate_docx_package(documents)
    with ZipFile(BytesIO(package)) as archive:
        assert set(archive.namelist()) == {"РПД.docx", "Учебный план.docx", "Характеристика.docx"}
        assert archive.read("Характеристика.docx") == doc_char
        assert "Трудоёмкость: 108 ч" in document_text(archive.read("РПД.docx"))
        assert "часов: 108 ч" in document_text(archive.read("Учебный план.docx"))


def test_evidence_pointing_outside_package_fails():
    originals = [
        OriginalDocument("doc-1", "RPD", "РПД.docx", make_docx(_make_paragraph("72 ч")))
    ]
    refs = [_ref("ref-1", "doc-missing", paragraph_index=1)]
    with pytest.raises(GenerationMappingError, match="вне пакета"):
        build_generation_documents([_change()], [_link()], refs, originals)


@pytest.mark.skipif(
    not (ORIGINALS / "РПД_матанализ1.docx").exists(), reason="нет реальных документов"
)
def test_real_rpd_document_roundtrip():
    docx = (ORIGINALS / "РПД_матанализ1.docx").read_bytes()
    paragraphs = read_area_text(docx, {})
    full_text = "".join(paragraphs)
    non_empty = [text for text in paragraphs if len(text.strip()) >= 10]
    target = next(text for text in non_empty if full_text.count(text) == 1)
    index = paragraphs.index(target) + 1
    refs = [_ref("ref-1", "doc-1", paragraph_index=index)]
    links = [FieldEvidence("entity", "e-1", "/attributes/name", "ref-1", {"value": target})]
    documents = build_generation_documents(
        [FieldChange("entity", "e-1", "/attributes/name", "ЗАМЕНА ПО ПЛАНУ")],
        links,
        refs,
        [OriginalDocument("doc-1", "RPD", "РПД_матанализ1.docx", docx)],
    )
    patched = patch_docx_bytes(docx, documents[0].patches)
    text = document_text(patched)
    assert "ЗАМЕНА ПО ПЛАНУ" in text
    assert target not in text
    with ZipFile(BytesIO(docx)) as original, ZipFile(BytesIO(patched)) as result:
        other_parts = [name for name in original.namelist() if name != "word/document.xml"]
        for name in other_parts:
            assert result.read(name) == original.read(name), name
