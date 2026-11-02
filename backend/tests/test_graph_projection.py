"""Контракт графа: изоляция версии, точность и сохранение исходных значений."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError


def example():
    first, second, relation = [str(uuid4()) for _ in range(3)]
    return {
        "schema_version": 2,
        "entities": [
            {
                "id": first,
                "kind": "discipline",
                "attributes": {
                    "code": "001.02",
                    "credits": "3.000000000000000001",
                    "unknown": None,
                    "nested": {"hours": [0, None, 108]},
                    "large": 2**90,
                },
            },
            {"id": second, "kind": "competency", "attributes": {"code": "ОПК-01"}},
        ],
        "relations": [
            {
                "id": relation,
                "source_id": first,
                "target_id": second,
                "type": "FORMS_COMPETENCY",
                "attributes": {"matrix_value": 0},
            }
        ],
    }


def test_projection_keeps_exact_values_real_edges_and_version_identity():
    from app.graph_projection import project_snapshot

    original = example()
    before = deepcopy(original)
    program, version = uuid4(), uuid4()
    graph = project_snapshot(program, version, original)
    assert original == before
    assert graph["node_count"] == 2 and graph["relation_count"] == 1
    first = next(
        n for n in graph["nodes"] if n["props"]["entity_id"] == original["entities"][0]["id"]
    )
    assert first["props"]["program_id"] == str(program)
    assert first["props"]["version_id"] == str(version)
    assert first["props"]["attr_code"] == "001.02"
    assert first["props"]["attr_credits"] == "3.000000000000000001"
    assert (
        json.loads(first["props"]["payload"])["attributes"] == original["entities"][0]["attributes"]
    )
    edge = graph["edges"][0]
    assert edge["type"] == "FORMS_COMPETENCY"
    assert edge["props"]["matrix_value"] == 0
    assert edge["source_id"] == original["relations"][0]["source_id"]
    assert edge["target_id"] == original["relations"][0]["target_id"]


def test_projection_rejects_dangling_edges_and_invalid_matrix_before_writing():
    from app.graph_projection import project_snapshot

    state = example()
    state["relations"][0]["target_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        project_snapshot(uuid4(), uuid4(), state)
    state = example()
    state["relations"][0]["attributes"]["matrix_value"] = True
    with pytest.raises(ValidationError):
        project_snapshot(uuid4(), uuid4(), state)


def test_business_relation_type_cannot_inject_cypher_and_is_not_lost():
    from app.graph_projection import project_snapshot

    state = example()
    malicious = "unsafe`) MATCH (n) DETACH DELETE n //"
    state["relations"][0]["type"] = malicious
    graph = project_snapshot(uuid4(), uuid4(), state)
    assert graph["edges"][0]["type"].startswith("R_")
    assert all(c in "R_0123456789abcdef" for c in graph["edges"][0]["type"])
    assert json.loads(graph["edges"][0]["props"]["payload"])["type"] == malicious


def test_graph_revision_and_hash_pin_exact_snapshot_including_evidence():
    from app.graph_projection import project_snapshot

    state = example()
    program, version = uuid4(), uuid4()
    first = project_snapshot(program, version, state)
    assert project_snapshot(program, version, state) == first
    state["entities"][0]["attributes"]["unknown"] = 0
    assert project_snapshot(program, version, state)["snapshot_hash"] != first["snapshot_hash"]
    assert project_snapshot(program, uuid4(), state)["nodes"] != first["nodes"]


@pytest.mark.parametrize("actual,expected", [(False, 0), (True, 1), (1.0, 1), (None, 0)])
def test_native_graph_property_comparison_preserves_types(actual, expected):
    from app.graph_store import same_properties

    assert not same_properties({"matrix_value": actual}, {"matrix_value": expected})
    assert same_properties({"matrix_value": expected}, {"matrix_value": expected})
