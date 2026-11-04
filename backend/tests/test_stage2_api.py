"""Приёмочные тесты второго этапа через настоящий HTTP и PostgreSQL."""

import copy
import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1", reason="Requires isolated Compose stack"
    ),
]


@pytest.fixture
def http():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as client:
        yield client


def create(http):
    response = http.post("/api/v1/programs", json={"name": "Artificial acceptance program"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def program(http):
    return create(http)


def url(program):
    return "/api/v1/programs/" + program["id"]


def snapshot():
    discipline, competency = str(uuid4()), str(uuid4())
    return {
        "schema_version": 1,
        "metadata": {},
        "entities": [
            {"id": discipline, "kind": "discipline", "attributes": {"name": "Test", "hours": 108}},
            {"id": competency, "kind": "competency", "attributes": {"code": "TEST-1"}},
        ],
        "relations": [
            {
                "id": str(uuid4()),
                "source_id": discipline,
                "target_id": competency,
                "type": "develops",
                "source_ref_ids": [],
                "confidence": None,
            }
        ],
        "source_refs": [],
        "document_ids": [],
    }


def save(http, program, state, expected=None, key=None):
    return http.post(
        url(program) + "/versions",
        headers={"Idempotency-Key": key or str(uuid4())},
        json={"expected_version_id": expected or program["current_version_id"], "snapshot": state},
    )


def test_create_program_has_persisted_empty_v1(http):
    program = create(http)
    assert http.get(url(program)).json() == program
    version = http.get(url(program) + "/versions/" + program["current_version_id"]).json()
    assert version["number"] == 1
    assert version["snapshot"]["entities"] == []
    assert any(item["id"] == program["id"] for item in http.get("/api/v1/programs").json())


def test_versions_are_immutable_and_restore_creates_new_version(http, program):
    state = snapshot()
    v2_response = save(http, program, state)
    assert v2_response.status_code == 201, v2_response.text
    v2 = v2_response.json()
    edited = copy.deepcopy(state)
    edited["entities"][1]["attributes"]["code"] = "TEST-2"
    edited["entities"][0]["attributes"]["hours"] = 144
    v3 = save(http, program, edited, expected=v2["id"]).json()
    assert v3["snapshot"]["entities"][1]["id"] == state["entities"][1]["id"]
    assert v3["snapshot"]["relations"] == state["relations"]
    assert http.get(url(program) + "/versions/" + v2["id"]).json()["snapshot"] == v2["snapshot"]
    restored = http.post(
        url(program) + "/versions/" + v2["id"] + "/restore",
        headers={"Idempotency-Key": str(uuid4())},
        json={"expected_version_id": v3["id"]},
    )
    assert restored.status_code == 201, restored.text
    assert restored.json()["number"] == 4
    assert restored.json()["snapshot"] == v2["snapshot"]
    assert restored.json()["restored_from_version_id"] == v2["id"]
    assert len(http.get(url(program) + "/versions").json()) == 4


def test_idempotency_precedes_stale_version_check(http, program):
    state, key = snapshot(), str(uuid4())
    first = save(http, program, state, key=key)
    assert first.status_code == 201, first.text
    repeat = save(http, program, state, key=key)
    assert repeat.status_code == 201
    assert first.json() == repeat.json()
    changed = copy.deepcopy(state)
    changed["metadata"]["different"] = True
    assert save(http, program, changed, key=key).status_code == 409
    assert len(http.get(url(program) + "/versions").json()) == 2


def test_concurrent_writers_only_create_one_version(http, program):
    state = snapshot()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: save(http, program, state), range(2)))
    assert sorted(r.status_code for r in responses) == [201, 409]
    versions = http.get(url(program) + "/versions").json()
    assert len(versions) == 2
    assert http.get(url(program)).json()["current_version_id"] == versions[0]["id"]


def test_invalid_links_and_cross_program_sources_leave_state_unchanged(http, program):
    invalid = snapshot()
    invalid["relations"][0]["target_id"] = str(uuid4())
    assert save(http, program, invalid).status_code == 422
    other = create(http)
    doc = http.post(
        url(other) + "/documents",
        files={"file": ("original.pdf", b"%PDF artificial", "application/pdf")},
    ).json()
    invalid = snapshot()
    invalid["document_ids"] = [doc["id"]]
    assert save(http, program, invalid).status_code == 422
    assert len(http.get(url(program) + "/versions").json()) == 1
    assert http.get(url(program)).json()["current_version_id"] == program["current_version_id"]
    assert http.get(url(program) + "/versions/" + other["current_version_id"]).status_code == 404
    assert http.get(url(program) + "/documents/" + doc["id"] + "/download").status_code == 404


def test_document_roundtrip_same_names_and_source_refs(http, program):
    docs = []
    for content in (b"%PDF first artificial", b"%PDF second artificial"):
        response = http.post(
            url(program) + "/documents", files={"file": ("same.pdf", content, "application/pdf")}
        )
        assert response.status_code == 201, response.text
        doc = response.json()
        assert doc["sha256"] == sha256(content).hexdigest()
        assert doc["size_bytes"] == len(content)
        assert "storage_key" not in doc
        downloaded = http.get(url(program) + "/documents/" + doc["id"] + "/download")
        assert downloaded.status_code == 200
        assert downloaded.content == content
        docs.append(doc)
    assert docs[0]["id"] != docs[1]["id"]
    assert len(http.get(url(program) + "/documents").json()) == 2
    state = snapshot()
    ref = str(uuid4())
    state["document_ids"] = [docs[0]["id"]]
    state["source_refs"] = [
        {"id": ref, "document_id": docs[0]["id"], "section": "Test", "locator": "page:1"}
    ]
    state["entities"][0]["source_ref_ids"] = [ref]
    result = save(http, program, state)
    assert result.status_code == 201, result.text
    assert result.json()["snapshot"]["source_refs"] == state["source_refs"]
    state["document_ids"] = []
    assert save(http, program, state, expected=result.json()["id"]).status_code == 422


@pytest.mark.parametrize(
    "filename,content,status",
    [
        ("empty.pdf", b"", 422),
        ("unsupported.exe", b"arbitrary", 415),
        ("oversized.pdf", b"x" * (50 * 1024 * 1024 + 1), 413),
    ],
)
def test_invalid_uploads_do_not_create_documents(http, program, filename, content, status):
    response = http.post(url(program) + "/documents", files={"file": (filename, content)})
    assert response.status_code == status, response.text
    assert http.get(url(program) + "/documents").json() == []


def test_all_declared_formats_preserve_bytes_without_claiming_parsing(http, program):
    for suffix in ("pdf", "docx", "xlsx"):
        content = b"Artificial storage test, intentionally not a valid document"
        response = http.post(
            url(program) + "/documents", files={"file": ("test." + suffix, content)}
        )
        assert response.status_code == 201, response.text
        doc = response.json()
        assert doc["format"] == suffix
        assert doc["parsing_status"] == "not_parsed"
        assert http.get(url(program) + "/documents/" + doc["id"] + "/download").content == content


def test_missing_key_unknown_ids_and_invalid_snapshot_are_rejected(http, program):
    assert (
        http.post(
            url(program) + "/versions",
            json={"snapshot": snapshot(), "expected_version_id": program["current_version_id"]},
        ).status_code
        == 422
    )
    assert http.get("/api/v1/programs/" + str(uuid4())).status_code == 404
    invalid = snapshot()
    invalid["entities"].append(copy.deepcopy(invalid["entities"][0]))
    assert save(http, program, invalid).status_code == 422
    invalid = snapshot()
    invalid["schema_version"] = 999
    assert save(http, program, invalid).status_code == 422
