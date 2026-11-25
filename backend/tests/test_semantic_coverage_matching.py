"""Сопоставление строки плана с РПД/РПП в R-09: имя, семестр и рег. номер."""

from worker.validation.semantic_coverage import CoverageDocument, _matching_documents

CURRICULUM = CoverageDocument(
    data={}, label="Учебный план.docx", role="curriculum", program_id=None
)


def _rpd(name, semesters, registration, label=None):
    return CoverageDocument(
        data={
            "disciplines": [
                {
                    "name": name,
                    "semesters": semesters,
                    "registration_number": registration,
                }
            ]
        },
        label=label or f"{name}.docx",
        role="rpd",
        program_id=None,
    )


def _item(name, semester, registration):
    return {"discipline_name": name, "semester": semester, "registration_number": registration}


def test_exact_name_and_semester_still_matches():
    matched, issue = _matching_documents(
        [_rpd("Моделирование информационных процессов", [6], "1877")],
        _item("Моделирование информационных процессов", 6, "1877"),
        CURRICULUM,
    )

    assert issue is None and len(matched) == 1


def test_abbreviated_rpd_name_matches_by_registration_number():
    # План пишет полное название, РПД — сокращённое; номер один и тот же.
    matched, issue = _matching_documents(
        [_rpd("Глубокое обучение в задачах NLP", [6], "82911")],
        _item("Глубокое обучение в задачах обработки естественного языка", 6, "82911"),
        CURRICULUM,
    )

    assert issue is None and len(matched) == 1


def test_practice_without_title_semesters_matches_both_plan_rows():
    # Семестры практики есть только в плане; на титуле РПП их нет (список пуст).
    practice = CoverageDocument(
        data={"disciplines": [{"name": "Учебная практика (научно-исследовательская работа)",
                               "semesters": [], "registration_number": "064683"}]},
        label="С6_064683_Учебная практика (научно-исследовательская работа).docx",
        role="rpp",
        program_id=None,
    )
    for semester in (6, 7):
        matched, issue = _matching_documents(
            [practice],
            _item("Учебная практика (научно-исследовательская работа)", semester, "64683"),
            CURRICULUM,
        )
        assert issue is None and len(matched) == 1


def test_broken_parenthesis_in_name_matches_by_registration_number():
    document = CoverageDocument(
        data={
            "disciplines": [
                {
                    "name": "Производственная практика (научно-исследовательская работа",
                    "semesters": [],
                    "registration_number": "64685",
                }
            ]
        },
        label="С8_064685_Производственная практика (научно-исследовательская работа).docx",
        role="rpp",
        program_id=None,
    )
    matched, issue = _matching_documents(
        [document],
        _item("Производственная практика (научно-исследовательская работа)", 8, "64685"),
        CURRICULUM,
    )

    assert issue is None and len(matched) == 1


def test_different_registration_number_still_reports_not_found():
    matched, issue = _matching_documents(
        [_rpd("Глубокое обучение в задачах NLP", [6], "82911")],
        _item("Глубокое обучение в задачах обработки естественного языка", 6, "82912"),
        CURRICULUM,
    )

    assert matched == [] and "не найден соответствующий РПД или РПП" in issue


def test_known_semesters_must_still_contain_the_plan_semester():
    # Номер совпал, но семестр строки плана вне разобранных — не совпадение.
    matched, issue = _matching_documents(
        [_rpd("Глубокое обучение в задачах NLP", [6], "82911")],
        _item("Глубокое обучение в задачах обработки естественного языка", 5, "82911"),
        CURRICULUM,
    )

    assert matched == [] and "не найден соответствующий РПД или РПП" in issue


def test_same_registration_in_two_documents_is_reported_as_ambiguous():
    documents = [
        CoverageDocument(
            data={"disciplines": [{"name": "Учебная практика (проектно-технологическая)",
                                   "semesters": [], "registration_number": "64682"}]},
            label="С5_Стандарт_064682.docx", role="rpp", program_id=None,
        ),
        CoverageDocument(
            data={"disciplines": [{"name": "Учебная практика (проектно-технологическая)",
                                   "semesters": [], "registration_number": "064682"}]},
            label="С6_064683_copy.docx", role="rpp", program_id=None,
        ),
    ]

    matched, issue = _matching_documents(
        documents,
        _item("Учебная практика (проектно-технологическая)", 5, "64682"),
        CURRICULUM,
    )

    assert matched == [] and "найдено несколько РПП" in issue


def test_program_scope_is_still_respected():
    other_program = _rpd("Глубокое обучение в задачах NLP", [6], "82911")
    other_program = CoverageDocument(data=other_program.data, label=other_program.label,
                                     role="rpd", program_id="other")
    curriculum = CoverageDocument(data={}, label="plan.docx", role="curriculum",
                                  program_id="mine")

    matched, issue = _matching_documents(
        [other_program],
        _item("Глубокое обучение в задачах NLP", 6, "82911"),
        curriculum,
    )

    assert matched == [] and "не найден соответствующий РПД или РПП" in issue
