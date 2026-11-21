import re

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from backend.contracts.document_v2.models import DocumentSection, ParseIssue

from .assessment import is_assessment_heading, is_score_scale_heading
from .common import normalize, read_section
from .content import is_content_heading


def _section_heading(text):
    return re.match(
        r"^(\d+(?:\.\d+)*)(?:\.\s*|\s+)([^\d\s].*)$", text.strip(), re.DOTALL,
    )


def _is_template_heading(paragraph, doc_type):
    label = re.sub(
        r"^(?:раздел\s+)?\d+(?:\.\d+)*\.?\s*", "", normalize(paragraph.text),
    ).replace("\u00ad", "").replace("-", "")
    return (
        is_content_heading(paragraph, doc_type)
        or is_score_scale_heading(paragraph)
        or is_assessment_heading(paragraph, doc_type)
        or label.startswith((
            "обеспечение учебных занятий",
            "обеспечение практики",
            "виды и формы текущего контроля",
            "виды и формы контроля",
            "методика контроля и аттестации",
            "методические материалы для оценки обучающимися содержания",
            "рекомендуемая форма отчета",
        ))
    )


def _section_boundaries(document, doc_type):
    positions = {p._p: index for index, p in enumerate(document.paragraphs)}
    boundaries = {
        p._p for p in document.paragraphs
        if not p.style.name.casefold().startswith(("toc", "оглавление"))
        and not re.search(r"\t\s*\d+\s*$", p.text)
        and _is_template_heading(p, doc_type)
    }
    last_boundary = max((positions[p] for p in boundaries), default=-1)

    def is_boundary(paragraph):
        if paragraph._p in boundaries:
            return True
        if positions[paragraph._p] < last_boundary:
            return False
        return None

    return is_boundary


def _block_text(block):
    if isinstance(block, Paragraph):
        return block.text

    return "\n".join(
        "\t".join(
            "\n".join(
                Paragraph(paragraph, block).text
                for paragraph in cell.iter(qn("w:p"))
            )
            for cell in row.tc_lst
        )
        for row in block._tbl.tr_lst
    )


def extract_document_sections(document, file_name, *, doc_type="RPD"):
    if doc_type not in {"RPD", "RPP"}:
        raise ValueError("Поддерживаются РПД и РПП")

    sections = []
    issues = []
    is_boundary = _section_boundaries(document, doc_type)
    headings = (
        (lambda p: is_content_heading(p, doc_type), "содержание занятий"),
        (is_score_scale_heading, "шкалы и критерии оценивания"),
        (lambda p: is_assessment_heading(p, doc_type), "оценочные средства"),
    )
    for is_heading, section_name in headings:
        blocks, _ = read_section(
            document,
            file_name,
            is_heading,
            section_name,
            keep_numbered_items=True,
            include_headings=True,
            is_boundary=is_boundary,
        )
        if not blocks:
            continue

        heading, source_ref = blocks[0]
        text = "\n\n".join(
            text for block, _ in blocks[1:]
            if (text := _block_text(block)).strip()
        )
        if any(
            block._element.xpath(
                ".//w:drawing | .//w:pict | .//w:object | .//w:sym | .//m:oMath"
            )
            for block, _ in blocks[1:]
        ):
            issues.append(ParseIssue(
                category="manual_review",
                message=(
                    "В разделе есть изображения, формулы или специальные символы, "
                    "не представленные обычным текстом DOCX; document_sections "
                    "содержит только доступный текст."
                ),
                source_ref=source_ref,
            ))
        if not text.strip():
            continue

        title = heading.text.strip()
        match = _section_heading(title)
        number = match[1] if match else ""
        sections.append(DocumentSection(
            source_ref=source_ref,
            section_number=number,
            title=match[2] if match else title,
            text=text,
            parent_section=number.rsplit(".", 1)[0] if "." in number else None,
        ))

    sections.sort(key=lambda section: section.source_ref.paragraph_index)
    return sections, issues
