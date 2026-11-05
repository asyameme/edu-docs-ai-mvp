"""Небольшие документы для проверки контрактов и переноса данных."""

from uuid import uuid4

from app.review_contract import PrepareRequest


def payload(kind="RPD", version=2):
    ref = {"file": "source.docx", "paragraph_index": 1}
    data = {
        "meta": {
            "schema_version": version,
            "doc_type": kind,
            "file_name": ref["file"],
            "file_hash": "a" * 64,
            "parser_config": "compatibility",
            "program_id": "26/5003/1",
        },
    }
    discipline = {
        "source_ref": ref,
        "name": "Дисциплина",
        "semesters": [1],
        "credit_units": 3,
        "hours_total": 108,
        "hours_by_kind": {"лекции": 36},
        "control_forms": ["зачёт"],
        "prerequisites": ["Основы"],
    }
    if kind in {"RPD", "RPP"}:
        data["disciplines"] = [discipline]
        data["document_sections"] = [
            {
                "source_ref": ref,
                "section_number": "1",
                "title": "Содержание",
                "text": "Исходный текст",
            }
        ]
        data["score_scale"] = [{"source_ref": ref, "grade_spbu": "зачёт", "criterion": "Выполнено"}]
    if kind == "RPP":
        data["semester_workload"] = [
            {
                "source_ref": ref,
                "semester": 1,
                "credit_units": 3,
                "hours_total": 108,
                "hours_by_kind": {"практика": 108},
                "control_forms": ["зачёт"],
            }
        ]
        data["practice_stages"] = [
            {"source_ref": ref, "name": "Подготовка", "description": "Описание этапа", "hours": 12}
        ]
    if kind == "CURRICULUM":
        data["curriculum_items"] = [
            {
                "source_ref": ref,
                "discipline_name": "Дисциплина",
                "semester": 1,
                "credit_units": 3,
                "hours_total": 108,
                "block": "Б1",
            }
        ]
    if kind == "MATRIX":
        data["matrix_cells"] = [
            {
                "source_ref": {"file": "matrix.xlsx", "sheet_name": "Лист1", "cell": "B2"},
                "discipline_name": "Дисциплина",
                "semester": 1,
                "competency_code": "УК-1",
                "value": 0,
            },
            {
                "source_ref": {"file": "matrix.xlsx", "sheet_name": "Лист1", "cell": "B3"},
                "discipline_name": "Дисциплина",
                "semester": 2,
                "competency_code": "УК-1",
                "value": 1,
            },
        ]
    if kind == "CKMV":
        data.update(
            competencies=[{"source_ref": ref, "code": "УК-1", "formulation": "Компетенция"}],
            professional_roles=[{"source_ref": ref, "local_id": "role1", "name": "Аналитик"}],
            role_competency_links=[
                {"source_ref": ref, "role_id": "role1", "competency_code": "УК-1"}
            ],
        )
    if kind == "CHARACTERISTIC":
        data["program"] = {
            "source_ref": ref,
            "name": "Программа",
            "profile": "Профиль",
            "education_languages": ["русский"],
            "program_cipher": "26/5003/1",
        }
    return data


def file_result(data, document_id=None):
    return {"document_id": document_id or uuid4(), "output": data}


def prepare(owner_id, parsed, **extra):
    return PrepareRequest.model_validate(
        {
            "job_id": uuid4(),
            "program_id": owner_id,
            "version_id": uuid4(),
            "base_snapshot": {},
            "documents": [{"document_id": uuid4(), "sha256": "a" * 64, "parsed": parsed, **extra}],
        }
    )
