import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document

from backend.contracts.document_v2.models import RPDOutput
from backend.worker.parser.discipline import organization_variant_rows
from backend.worker.parser.parse_rpd import parse_rpd


def working_program(variants, *, sequential=False, total=None, misplaced=False):
    document = Document()
    for text in (
        "РАБОЧАЯ ПРОГРАММА УЧЕБНОЙ ДИСЦИПЛИНЫ",
        "Проверочная дисциплина",
        "Трудоемкость в зачетных единицах: 7",
        "1.2. Пререквизиты",
        "Предшествующие дисциплины: «Алгебра».",
        "1.3. Перечень результатов обучения (learning outcomes)",
    ):
        document.add_paragraph(text)
    table = document.add_table(rows=2, cols=3)
    for cell, text in zip(table.rows[0].cells, (
        "Код и наименование компетенции", "Планируемые результаты обучения", "Код индикатора и индикатор достижения",
    )):
        cell.text = text
    for cell, text in zip(table.rows[1].cells, (
        "ПКП-УНК-03. Способен применять системный подход",
        "ПКП-УНК-03.01. У-1. Умеет анализировать взаимосвязи задач.",
        "ПКП-УНК-03" if misplaced else "ПКП-УНК-03.01. Демонстрирует системное мышление",
    )):
        cell.text = text
    document.add_paragraph("2.1. Организация учебных занятий")
    document.add_paragraph("2.1.1. Основной курс")
    table = document.add_table(rows=2, cols=5)
    for row, values in zip(table.rows, (
        ("Период обучения", "Контактная работа обучающихся с преподавателем", "Самостоятельная работа", "Объем активных и интерактивных форм", "Трудоемкость"),
        ("Период обучения", "лекции", "сам.раб. с использованием методических материалов", "Активные формы", "Трудоемкость"),
    )):
        for cell, value in zip(row.cells, values):
            cell.text = value
    for track, semester, lecture, independent, active, credits in variants:
        cells = table.add_row().cells
        cells[0].merge(cells[-1]).text = track
        cells = table.add_row().cells
        cells[0].merge(cells[-1]).text = "Форма обучения: очная"
        row_values = (str(lecture), str(independent), str(active), str(credits))
        for label in ([f"Семестр {semester}"] if sequential else [f"Семестр {semester}", "ИТОГО"]):
            for cell, value in zip(table.add_row().cells, (label, *row_values)):
                cell.text = value
    if total is not None:
        for cell, value in zip(table.add_row().cells, ("ИТОГО", *map(str, total))):
            cell.text = value
    document.add_paragraph("2.2. Структура и содержание учебных занятий")
    table = document.add_table(rows=2, cols=3)
    for row, values in zip(table.rows, (
        ("Тема", "Вид занятий", "Количество часов"), ("Тема курса", "лекции", "4"),
    )):
        for cell, value in zip(row.cells, values):
            cell.text = value
    document.add_paragraph("3.1.4. Оценочные средства")
    table = document.add_table(rows=2, cols=2)
    for row, values in zip(table.rows, (
        ("Код индикатора и индикатор достижения", "Оценочные средства"),
        ("ПКП-УНК-03.01. Демонстрирует системное мышление", "Вопросы к экзамену"),
    )):
        for cell, value in zip(row.cells, values):
            cell.text = value
    document.add_paragraph("3.1.5. Методические материалы для оценки обучающимися содержания")
    return document


class TestWorkloadVariants(unittest.TestCase):
    profiles = (
        ("ОСНОВНАЯ ТРАЕКТОРИЯ Профиль ТОП-ИТ ПРО", 3, 8, 28, 20, 1),
        ("ОСНОВНАЯ ТРАЕКТОРИЯ Профиль СТАНДАРТ", 4, 10, 62, 40, 2),
    )

    def parse(self, document, **kwargs):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "working.docx"
            document.save(path)
            return parse_rpd(path, program_id="test-program", **kwargs)

    def test_both_profiles_keep_independent_values_and_sources(self):
        output = self.parse(working_program(self.profiles))
        self.assertEqual(
            [(d.semesters, d.credit_units, d.hours_total, d.source_ref.table, d.source_ref.row) for d in output.disciplines],
            [([3], 1, 36, 2, 6), ([4], 2, 72, 2, 10)],
        )
        self.assertEqual([d.prerequisites for d in output.disciplines], [["Алгебра"], ["Алгебра"]])
        self.assertEqual(len({d.name for d in output.disciplines}), 1)
        self.assertEqual(output.course_content[0].topic, "Тема курса")
        for index, profile in enumerate(("топ-ит про", "стандарт")):
            issue, = [i for i in output.parse_issues if f"disciplines[{index}]" in i.message]
            self.assertIn(profile, issue.message)
            self.assertEqual(issue.source_ref, output.disciplines[index].source_ref)
        self.assertEqual(RPDOutput.model_validate_json(output.model_dump_json()), output)

    def test_explicit_profile_selection_remains_available(self):
        output = self.parse(working_program(self.profiles), workload_track=self.profiles[1][0])
        self.assertEqual(len(output.disciplines), 1)
        self.assertEqual(output.disciplines[0].hours_total, 72)
        self.assertEqual(output.disciplines[0].semesters, [4])

    def test_invalid_profile_does_not_drop_the_other_profile(self):
        profiles = (self.profiles[0], (*self.profiles[1][:-1], "2,5"))
        output = self.parse(working_program(profiles))
        self.assertEqual(len(output.disciplines), 2)
        self.assertEqual(output.disciplines[0].hours_total, 36)
        self.assertIsNone(output.disciplines[1].credit_units)
        self.assertTrue(any(i.category == "unexpected_format" for i in output.parse_issues))

    def test_sequential_semesters_use_explicit_total_and_keep_title_credits(self):
        variants = (
            ("ТРАЕКТОРИЯ 7 СЕМЕСТРА Профиль Стандарт", 7, 8, 28, 20, 1),
            ("ТРАЕКТОРИЯ 8 СЕМЕСТРА Профиль Стандарт", 8, 10, 62, 40, 2),
        )
        output = self.parse(working_program(variants, sequential=True, total=(20, 90, 60, 6)))
        self.assertEqual(len(output.disciplines), 1)
        discipline = output.disciplines[0]
        self.assertEqual(discipline.semesters, [7, 8])
        self.assertEqual((discipline.hours_total, discipline.credit_units), (110, 6))
        self.assertEqual(discipline.source_ref.row, 9)
        self.assertEqual(output.title_credit_units, 7)
        self.assertNotIn("активные формы", discipline.hours_by_kind)

    def test_semester_captions_with_different_profiles_are_not_combined(self):
        variants = (
            ("ТРАЕКТОРИЯ 7 СЕМЕСТРА Профиль ТОП-ИТ ПРО", 7, 8, 28, 20, 1),
            ("ТРАЕКТОРИЯ 8 СЕМЕСТРА Профиль Стандарт", 8, 10, 62, 40, 2),
        )
        output = self.parse(working_program(variants))
        self.assertEqual([(d.semesters, d.hours_total) for d in output.disciplines], [([7], 36), ([8], 72)])

    def test_mismatched_semester_captions_are_not_combined(self):
        variants = (
            ("ТРАЕКТОРИЯ 7 СЕМЕСТРА Профиль Стандарт", 8, 8, 28, 20, 1),
            ("ТРАЕКТОРИЯ 8 СЕМЕСТРА Профиль Стандарт", 7, 10, 62, 40, 2),
        )
        document = working_program(variants)
        rows, _ = organization_variant_rows(document, "working.docx")
        self.assertEqual(len({track for (table, row), (track, _) in rows.items() if table == 2 and row in (5, 9)}), 2)

    def test_indicator_is_confirmed_by_outcome_code_and_explicit_description(self):
        output = self.parse(working_program(self.profiles[:1], misplaced=True))
        indicator, = [i for i in output.indicators if i.code == "ПКП-УНК-03.01"]
        self.assertEqual(indicator.text, "Демонстрирует системное мышление")
        self.assertEqual((indicator.source_ref.table, indicator.source_ref.row, indicator.source_ref.col), (4, 2, 1))
        self.assertEqual(output.learning_outcomes[0].text, "Умеет анализировать взаимосвязи задач.")
        self.assertTrue(any(r.src_entity == "competency" and r.dst_key == indicator.code for r in output.relations))
        self.assertTrue(any(r.src_entity == "indicator" and r.src_key == indicator.code and r.dst_key == "Вопросы к экзамену" for r in output.relations))

    def test_indicator_is_not_invented_when_description_is_absent(self):
        document = working_program(self.profiles[:1], misplaced=True)
        document.tables[3].cell(1, 0).text = "ПКП-УНК-03.01"
        output = self.parse(document)
        self.assertNotIn("ПКП-УНК-03.01", {i.code for i in output.indicators})
        self.assertFalse(any(r.src_entity == "indicator" for r in output.relations))

    def test_conflicting_indicator_descriptions_are_not_chosen_arbitrarily(self):
        document = working_program(self.profiles[:1], misplaced=True)
        row = document.tables[3].add_row()
        row.cells[0].text = "ПКП-УНК-03.01. Другое описание индикатора"
        row.cells[1].text = "Контрольная работа"
        output = self.parse(document)
        self.assertNotIn("ПКП-УНК-03.01", {i.code for i in output.indicators})
        self.assertTrue(any("разные описания индикатора" in i.message for i in output.parse_issues))


if __name__ == "__main__":
    unittest.main()
