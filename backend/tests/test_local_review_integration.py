"""HTTP/Temporal с настоящими DOCX, парсерами и валидатором без ML-фикстур."""

import io
import os
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from docx import Document
from test_review_integration import post, wait

from app.local_parser import parse_local_document
from worker.validation.validator import load_rules

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("LOCAL_REVIEW_TESTS") != "1", reason="Требуется изолированный локальный стек"
    ),
]
ORIGINALS = Path(__file__).resolve().parents[2] / "tests" / "originals"


def test_real_documents_prepare_choice_check_and_docx():
    original = (ORIGINALS / "РПД_матанализ1.docx").read_bytes()
    parsed = parse_local_document("РПД/source.docx", original, uuid4())
    formulation = parsed["competencies"][0]["formulation"]
    alternative = "способен создавать прогнозы на основе статистических моделей"
    word = Document(io.BytesIO(original))
    matches = [
        c for table in word.tables for row in table.rows for c in row.cells if formulation in c.text
    ]
    assert len(matches) == 1
    matches[0].text = matches[0].text.replace(formulation, alternative)
    changed = io.BytesIO()
    word.save(changed)
    members = {
        "РПД/изменённая.docx": changed.getvalue(),
        "Характеристика/Общая характеристика.docx": (
            ORIGINALS / "Общая характеристика.docx"
        ).read_bytes(),
    }
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = post(http, "/api/v1/programs", {"name": "Настоящий локальный валидатор"}, 201)
        root = "/api/v1/programs/" + program["id"]
        uploaded = http.post(
            root + "/imports",
            files={"file": ("real.zip", package.getvalue())},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert uploaded.status_code == 202, uploaded.text
        imported = wait(http, root + "/imports/" + uploaded.json()["id"])
        # Исходный РПД содержит известное предупреждение о неполной шкале оценивания.
        assert imported["status"] == "partial", imported
        assert all(item["status"] == "completed" for item in imported["files"])
        assert not any(item["severity"] == "error" for item in imported["issues"])
        prepared = post(
            http,
            root + "/reviews",
            {
                "import_job_id": imported["id"],
                "expected_version_id": program["current_version_id"],
            },
            202,
        )
        job = wait(http, root + "/reviews/" + prepared["id"])
        assert job["status"] == "completed", job
        version = http.get(root + "/versions/" + job["output_version_id"]).json()
        assert version["snapshot"]["entities"]
        assert version["snapshot"]["review"]["graph_ready"] is True
        details = http.get(root + f"/versions/{version['id']}/review").json()
        issues = [i for i in details["issues"] if i["requires_decision"]]
        assert len(issues) == 1
        issue = issues[0]
        assert issue["rule_code"] == "R-07"
        variant = next(
            v for v in issue["variants"] if all(c["value"] != alternative for c in v["changes"])
        )
        selected = variant["changes"][0]["value"]
        version = post(
            http,
            root + f"/reviews/{job['id']}/decisions",
            {
                "expected_version_id": version["id"],
                "conflict_id": issue["id"],
                "variant_id": variant["id"],
                "signature": "Локальная приёмка",
            },
            201,
        )
        assert version["snapshot"]["review"]["graph_ready"] is True
        assert any(
            link["observed_value"].get("value") == alternative
            for link in version["snapshot"]["source_links"]
        )
        checked = post(
            http, root + f"/reviews/{job['id']}/checks", {"version_id": version["id"]}, 202
        )
        checked = wait(http, root + f"/reviews/{job['id']}/checks/" + checked["id"])
        assert checked["status"] == "completed", checked
        assert all("checks" not in entry for entry in checked["report"]["coverage"])
        assert all(
            "evidence" not in issue and "checks" not in issue
            for issue in checked["report"]["issues"]
        )
        details = http.get(root + f"/versions/{version['id']}/review").json()
        report = details["check_report"]
        coverage = {entry["rule_code"]: entry for entry in report["coverage"]}
        assert set(coverage) == {
            rule["id"] for rule in load_rules()["rules"]
            if rule["status"] != "draft"
            and "graph_validation" in rule["phases"]
            and rule.get("run_mode", "automatic") == "automatic"
        }
        assert len(coverage) == 13
        assert "R-07" not in coverage
        assert "R-01-graph" in coverage
        assert all("checks" not in entry for entry in report["coverage"])
        assert all("evidence" not in issue and "checks" not in issue for issue in report["issues"])
        assert not any(i["severity"] == "blocking" for i in report["issues"])
        assert all("evidence" not in issue and "checks" not in issue for issue in details["issues"])
        assert details["lifecycle"]["can_generate"]
        confirmed = version
        generated = post(http, root + "/generation", {"version_id": confirmed["id"]}, 202)
        generated = wait(http, root + "/generation/" + generated["id"])
        assert generated["status"] == "completed", generated
        archive = http.get(root + "/generation/" + generated["id"] + "/download")
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as files:
            rendered = Document(io.BytesIO(files.read("изменённая.docx")))
            text = "\n".join(c.text for t in rendered.tables for r in t.rows for c in r.cells)
            assert selected in text and alternative not in text
