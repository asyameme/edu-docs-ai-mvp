"""Итог проверки для чата — готовыми словами, без двусмысленного флага."""

import asyncio
from uuid import uuid4

from app.chat_validation import check_summary
from app.domain import Entity, SnapshotV2
from worker.agent.contracts import ChangeProposal
from worker.agent.read_adapter import PostgresChatReadPort
from worker.agent.read_port import configure_worker_read_port


def test_summary_counts_rules_by_status():
    summary = check_summary(
        "completed", {"passed": 9, "failed": 3, "skipped": 1}, False, False
    )

    assert summary == "Правил в проверке: 9 пройдено, 3 с замечаниями, 1 пропущено."


def test_summary_for_fully_passed_and_unfinished_checks():
    assert check_summary("completed", {"passed": 4}, True, False).endswith(
        "Все правила пройдены."
    )
    assert "частично" in check_summary("partial", {"passed": 2}, False, False)
    assert "выводов о правилах делать нельзя" in check_summary("pending", {}, None, False)
    assert "выводов о правилах делать нельзя" in check_summary("failed", {}, None, False)
    assert "Показана только часть" in check_summary(
        "completed", {"passed": 2, "failed": 1}, False, True
    )
    assert check_summary("completed", {"passed": 4}, False, True).endswith(
        "Все правила пройдены. Показана только часть подробностей."
    )


def test_workload_by_kind_is_not_cut_to_the_first_entries():
    """Часы по видам нагрузки должны доходить до модели целиком."""
    identity = uuid4()
    kinds = {
        "лекции": 40,
        "семинары": 40,
        "практические занятия": 40,
        "промежуточная аттестация": 4,
        "промежуточная аттестация (сам.раб.)": 14,
        "сам.раб. с использованием методических материалов": 78,
    }
    port = PostgresChatReadPort(None)
    port.snapshot = lambda *_: SnapshotV2(
        entities=[
            Entity(
                id=identity,
                kind="discipline",
                attributes={
                    "name": "Технологии MLOps",
                    "hours_total": 216,
                    "credit_units": 6,
                    "control_forms": ["зачет"],
                    "hours_by_kind": kinds,
                    "prerequisites": [],
                    "registration_number": "82909",
                },
            )
        ]
    )
    configure_worker_read_port(port)

    result = asyncio.run(
        port.read_entity(program_id=uuid4(), version_id=uuid4(), entity_id=identity)
    )

    assert result.entity.attributes["hours_by_kind"] == kinds


def test_proposal_drops_only_redundant_confirmation():
    proposal = ChangeProposal.model_validate({
        "kind": "change_proposal",
        "summary": "Обновить объём",
        "changes": [{
            "target_kind": "entity", "target_id": uuid4(), "field_path": "hours_total",
            "expected_old_value": 108, "new_value": 109, "reason": "Уточнение",
            "source_ref_ids": [uuid4()],
        }],
        "unresolved_questions": [
            "Подтверждаете применение правки: общий объём часов 108 → 109?",
            "Какой семестр изменить?",
        ],
    })

    assert proposal.unresolved_questions == ["Какой семестр изменить?"]
