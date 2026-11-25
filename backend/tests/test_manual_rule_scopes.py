"""Ручные семантические проверки ограничиваются выбранной целью."""

from worker.validation.validator import Document, load_rules, run_validation


def test_manual_r11_checks_only_the_selected_rpd(monkeypatch):
    checked_documents = []

    def evaluate_rpd_alignment(*, document_label, **_kwargs):
        checked_documents.append(document_label)
        return 1, []

    monkeypatch.setattr(
        "worker.validation.validator.evaluate_rpd_alignment",
        evaluate_rpd_alignment,
    )
    documents = [
        Document(
            {"meta": {"doc_type": "RPD", "file_name": "Первая РПД.docx"}},
            "Первая РПД.docx",
            "document-1",
        ),
        Document(
            {"meta": {"doc_type": "RPD", "file_name": "Выбранная РПД.docx"}},
            "Выбранная РПД.docx",
            "document-2",
        ),
    ]
    rules = load_rules()

    run_validation(
        documents,
        rules,
        phase="graph_validation",
        rule_codes=["R-11"],
        document_ids=["document-2"],
    )

    assert checked_documents == ["Выбранная РПД.docx"]
