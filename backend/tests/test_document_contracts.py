"""Совместимость версий и принадлежность документов программе."""

from uuid import uuid4

import pytest
from document_cases import payload, prepare
from pydantic import ValidationError

from app.parser_adapter import to_snapshot
from app.parser_document import parse_output
from app.review_contract import AnalysisDocument


def test_backend_schema_matches_models_without_regenerating_team_contracts():
    import json

    from scripts.export_contract_schemas import schemas

    generated = schemas()
    assert len(generated) == 1
    for path, schema in generated.items():
        assert path.name == "contract.schema.json" and path.parent.name == "review"
        assert json.loads(path.read_text(encoding="utf-8")) == schema, str(path)


def test_parser_v1_is_rejected_at_every_backend_entry():
    data = payload(version=1)
    data.pop("document_sections")
    with pytest.raises(ValueError):
        parse_output(data)
    with pytest.raises(ValidationError):
        prepare(uuid4(), data)
    with pytest.raises(ValueError):
        to_snapshot(uuid4(), [{"document_id": uuid4(), "output": data}])


def test_analysis_schema_offers_only_v2_document_models():
    schema = AnalysisDocument.model_json_schema()
    assert "ParserOutput" not in schema["$defs"]
    assert len(schema["properties"]["parsed"]["anyOf"]) == 6


def test_backend_uses_v2_one_based_paragraph_coordinates_without_shifting():
    from backend.contracts.document_v2.models import parse_document

    data = payload()
    data["disciplines"][0]["source_ref"]["paragraph_index"] = 0
    with pytest.raises(ValidationError):
        prepare(uuid4(), data)
    data["disciplines"][0]["source_ref"]["paragraph_index"] = 1
    result = prepare(uuid4(), data)
    assert result.documents[0].parsed.disciplines[0].source_ref.paragraph_index == 1
    assert result.documents[0].parsed.model_dump() == parse_document(data).model_dump()


def test_analysis_envelope_does_not_add_another_program_id_to_document():
    result = prepare(uuid4(), payload()).model_dump(mode="json")
    assert "program_id" not in result["documents"][0]


@pytest.mark.parametrize("kind", ["RPD", "RPP", "MATRIX", "CKMV", "CHARACTERISTIC"])
def test_backend_accepts_v2_default_version_from_main_models(kind):
    from backend.contracts.document_v2.models import parse_document

    data = payload(kind)
    data["meta"].pop("schema_version")
    result = prepare(uuid4(), data)
    assert result.documents[0].parsed.model_dump() == parse_document(data).model_dump()


@pytest.mark.parametrize("kind", ["RPD", "RPP", "CURRICULUM", "MATRIX", "CKMV", "CHARACTERISTIC"])
def test_analysis_accepts_each_v2_type_without_replacing_academic_code(kind):
    data = payload(kind)
    result = prepare(uuid4(), data)
    assert result.documents[0].parsed.meta.schema_version == 2
    assert result.documents[0].parsed.meta.program_id == "26/5003/1"
    assert result.documents[0].parsed.model_dump(mode="json")["meta"]["file_hash"] == "a" * 64


def test_undocumented_program_id_in_document_envelope_is_rejected():
    with pytest.raises(ValidationError):
        prepare(uuid4(), payload(), program_id=uuid4())


def test_legacy_uuid_ownership_is_still_checked():
    data = payload()
    data["meta"]["program_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        prepare(uuid4(), data)


def test_unknown_versions_and_negative_coordinates_are_rejected():
    with pytest.raises(ValidationError):
        AnalysisDocument(document_id=uuid4(), sha256="a" * 64, parsed=payload(version=3))
    data = payload()
    data["disciplines"][0]["source_ref"]["paragraph_index"] = -1
    with pytest.raises(ValidationError):
        AnalysisDocument(document_id=uuid4(), sha256="a" * 64, parsed=data)


@pytest.mark.parametrize("value", [True, False, "1", 2])
def test_matrix_cell_is_rejected_before_storage_when_not_integer_zero_or_one(value):
    data = payload("MATRIX")
    data["matrix_cells"][0]["value"] = value
    with pytest.raises(ValidationError):
        AnalysisDocument(document_id=uuid4(), sha256="a" * 64, parsed=data)


@pytest.mark.parametrize("cells", [None, 1, {"value": 0}])
def test_invalid_matrix_collection_is_a_contract_error(cells):
    data = payload("MATRIX")
    data["matrix_cells"] = cells
    with pytest.raises(ValidationError):
        AnalysisDocument(document_id=uuid4(), sha256="a" * 64, parsed=data)
