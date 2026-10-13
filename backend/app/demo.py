from app.models import DemoProgram


def demo_program() -> DemoProgram:
    return DemoProgram.model_validate(
        {
            "is_demo": True,
            "notice": "Искусственный пример: файлы не загружены, анализ не выполнялся.",
            "program": {"id": "demo-program", "name": "Демонстрационная образовательная программа"},
            "version": {"id": "demo-version-1", "number": 1, "status": "draft"},
            "disciplines": [
                {
                    "id": "demo-discipline",
                    "name": "Программирование",
                    "semesters": [1],
                    "hours": None,
                }
            ],
            "competencies": [
                {
                    "id": "demo-competency",
                    "code": "ОПК-DEMO",
                    "name": "Пример компетенции, не нормативное требование",
                    "type": "demo",
                }
            ],
            "source_refs": [
                {
                    "id": "source-plan",
                    "document_id": "demo-plan",
                    "section": "Дисциплины",
                    "locator": "sheet:План!D12",
                },
                {
                    "id": "source-rpd",
                    "document_id": "demo-rpd",
                    "section": "Трудоёмкость",
                    "locator": "table:2/row:3/cell:2",
                },
            ],
            "relations": [
                {
                    "source_id": "demo-discipline",
                    "target_id": "demo-competency",
                    "type": "forms_competency",
                    "source_ref_ids": ["source-rpd"],
                }
            ],
            "conflicts": [
                {
                    "id": "demo-conflict",
                    "entity_id": "demo-discipline",
                    "field": "hours",
                    "status": "unresolved",
                    "options": [
                        {"value": 108, "source_ref_id": "source-plan"},
                        {"value": 144, "source_ref_id": "source-rpd"},
                    ],
                }
            ],
        }
    )
