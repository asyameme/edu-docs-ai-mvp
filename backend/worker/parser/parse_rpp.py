import argparse
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from .common import heading_level, normalize
from .models import ParseIssue, ParserMeta, ParserOutput, SourceRef
from .results import extract_results_into

from .discipline import build_practice_discipline

def parse_rpp(file_path):
    file_path = Path(file_path)

    if file_path.suffix.lower() != ".docx":
        raise ValueError("Ожидается файл DOCX")

    raw = file_path.read_bytes()
    document = Document(BytesIO(raw))
    title_parts = []

    # Читаем титул до первого раздела.
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            if heading_level(block) is not None:
                break

            title_parts.append(block.text)

        elif isinstance(block, Table):
            title_parts.extend(
                cell.text
                for row in block.rows
                for cell in row.cells
            )

    title = re.sub(
        r"\s+",
        "",
        "".join(title_parts),
    ).casefold()

    if (
        "рабочаяпрограммапрактики" not in title
        or "рабочаяпрограммаучебнойдисциплины" in title
    ):
        raise ValueError(
            "Не удалось однозначно распознать РПП по титулу"
        )

    output = ParserOutput(
        meta=ParserMeta(
            doc_type="RPP",
            file_name=file_path.name,
            file_hash=sha256(raw).hexdigest(),
            parser_config="rpp_results_v1",
        )
    )

    output.parse_issues.append(ParseIssue(
        category="manual_review",
        severity="info",
        message=(
            "Частичный разбор РПП: извлекаются компетенции, индикаторы "
            "и результаты обучения из таблиц и связи между ними. "
            "Текстовые перечни компетенций, нагрузка, содержание "
            "и оценочные материалы пока не извлекаются."
        ),
        source_ref=SourceRef(file=file_path.name),
    ))

    # Ищем раздел по тексту заголовка.
    candidates = []

    for paragraph in document.paragraphs:
        if paragraph.style.name.casefold().startswith((
            "toc", "оглавление",
        )):
            continue

        if re.search(r"\t\s*\d+\s*$", paragraph.text):
            continue

        text = re.sub(
            r"^\d+(?:\.\d+)*\.?\s*",
            "",
            normalize(paragraph.text),
        )

        if text.rstrip(":") == (
            "перечень профессиональных компетенций, "
            "формирующих практическую составляющую "
            "результатов освоения программы"
        ):
            candidates.append(paragraph)

    # Заголовок может повторяться у раздела 1.7
    # и вложенного подраздела 1.7.2.
    depth = min(
        (heading_level(p) or 2 for p in candidates),
        default=2,
    )

    roots = {
        p._p
        for p in candidates
        if (heading_level(p) or 2) == depth
    }
    discipline, workload_issues = build_practice_discipline(
        document,
        file_path.name,
        title_parts,
    )
    output.parse_issues.extend(workload_issues)

    if discipline is not None:
        output.disciplines.append(discipline)
        
    output = extract_results_into(
        output,
        document,
        file_path.name,
        lambda paragraph: paragraph._p in roots,
    )

    return ParserOutput.model_validate(output.model_dump())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "-o", "--output",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    if args.input.resolve() == args.output.resolve():
        parser.error(
            "Выходной файл должен отличаться от исходного"
        )

    try:
        output = parse_rpp(args.input)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    text = output.model_dump_json(indent=2)
    ParserOutput.model_validate_json(text)
    args.output.write_text(text, encoding="utf-8")

    print(
        f"Компетенций: {len(output.competencies)}. "
        f"Индикаторов: {len(output.indicators)}. "
        f"Результатов обучения: {len(output.learning_outcomes)}."
    )


if __name__ == "__main__":
    main()