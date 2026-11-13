from __future__ import annotations
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DocType = Literal[
    "RPD",
    "RPP",
    "CURRICULUM",
    "MATRIX",
    "CHARACTERISTIC",
    "CKMV",
]
Severity = Literal["error", "warning", "info"]
IssueCategory = Literal[
    "not_found",
    "unparsed_row",
    "empty_cell",
    "unexpected_format",
    "ambiguous",
    "manual_review",
]
EntityName = Literal[
    "program",
    "discipline",
    "competency",
    "indicator",
    "learning_outcome",
    "assessment_tool",
    "curriculum_item",
    "course_topic",
]
RelationKind = Literal["формирует", "имеет", "проверяется", "включает"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRefV2(ContractModel):
    """Координаты значения в исходном DOCX или XLSX.

    paragraph_index считает абзацы тела DOCX с 1, включая таблицы и исключая
    колонтитулы и сноски. Номера table, row и col также начинаются с 1;
    row=1 обозначает заголовок таблицы. Для XLSX используются sheet_name и cell.
    Текст фрагмента хранится отдельно от ссылки.
    """

    file: str
    section: str | None = None
    paragraph_index: int | None = Field(default=None, ge=1)
    table: int | None = None
    row: int | None = None
    col: int | None = None
    sheet_name: str | None = None
    cell: str | None = None

    @field_validator("file")
    @classmethod
    def _file_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_ref.file пуст")
        return value


SourceRef = SourceRefV2


class BaseEntity(ContractModel):
    source_ref: SourceRefV2


class Program(BaseEntity):
    name: str | None = None
    level: Literal["бакалавриат", "магистратура", "специалитет", "аспирантура"] | None = None
    direction_code: str | None = None
    education_standard: str | None = None
    professional_standards: list[str] = []


class Discipline(BaseEntity):
    name: str
    semesters: list[int]
    credit_units: int | None = Field(ge=1)
    hours_total: int | None = Field(ge=0)
    hours_by_kind: dict[str, int]
    control_forms: list[str] = []
    prerequisites: list[str] = []

    @field_validator("semesters")
    @classmethod
    def _semesters_1_8(cls, value: list[int]) -> list[int]:
        bad = [semester for semester in value if not 1 <= semester <= 8]
        if bad:
            raise ValueError(f"семестры вне 1–8: {bad}")
        return value


class Competency(BaseEntity):
    code: str
    formulation: str

    @field_validator("code")
    @classmethod
    def _code_no_spaces(cls, value: str) -> str:
        if not value.strip() or len(value.split()) > 1:
            raise ValueError("код компетенции пуст или содержит пробелы")
        return value


class Indicator(BaseEntity):
    code: str
    text: str
    competency_code: str


class LearningOutcome(BaseEntity):
    local_id: str
    text: str
    kind: Literal["знать", "уметь", "владеть"] | None = None
    code: str | None = None

    @field_validator("local_id")
    @classmethod
    def _id_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("local_id пуст")
        return value


class AssessmentTool(BaseEntity):
    name: str


class CurriculumItem(BaseEntity):
    discipline_name: str
    semester: int = Field(ge=1, le=8)
    credit_units: int = Field(ge=1)
    hours_total: int = Field(ge=0)
    block: str


class AcademicHoursRange(ContractModel):
    kind: Literal["range"]
    min: int = Field(ge=0, strict=True)
    max: int = Field(ge=0, strict=True)
    unit: Literal["academic_hour"]
    raw_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_bounds(self):
        if self.min > self.max:
            raise ValueError("Нижняя граница часов больше верхней")
        return self


class CourseTopic(BaseEntity):
    topic: str
    activity: str
    hours: Annotated[int, Field(ge=0)] | AcademicHoursRange | None
    module: str | None = None


class ScoreScaleItem(BaseEntity):
    grade_spbu: str
    grade_ects: str | None = None
    points: str | None = None
    criterion: str | None = None


class Relation(BaseEntity):
    src_entity: EntityName
    src_key: str
    dst_entity: EntityName
    dst_key: str
    kind: RelationKind

    @field_validator("src_key", "dst_key")
    @classmethod
    def _key_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ключ связи пуст")
        return value


class ParseIssue(ContractModel):
    category: IssueCategory
    message: str
    severity: Severity = "warning"
    source_ref: SourceRefV2 | None = None


class MetaV2(ContractModel):
    schema_version: Literal[2] = 2
    doc_type: DocType
    file_name: str
    file_hash: str
    parser_config: str
    program_id: str | None = None

    @field_validator("file_hash")
    @classmethod
    def _hash_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            raise ValueError("file_hash должен быть sha256 (64 hex-символа)")
        return value.lower()

class RPDMeta(MetaV2):
    doc_type: Literal['RPD'] = 'RPD'
    program_id: str
class RPPMeta(MetaV2):
    doc_type: Literal['RPP'] = 'RPP'
class CurriculumMeta(MetaV2):
    doc_type: Literal['CURRICULUM'] = 'CURRICULUM'
class MatrixMeta(MetaV2):
    doc_type: Literal['MATRIX'] = 'MATRIX'
class CKMVMeta(MetaV2):
    doc_type: Literal['CKMV'] = 'CKMV'
class CharacteristicMeta(MetaV2):
    doc_type: Literal['CHARACTERISTIC'] = 'CHARACTERISTIC'

class DocumentSection(ContractModel):
    source_ref: SourceRef
    section_number: str
    title: str
    text: str
    parent_section: str | None = None

class SemesterWorkload(ContractModel):
    source_ref: SourceRef
    semester: int = Field(ge=1, le=8)
    credit_units: int = Field(ge=0)
    hours_total: int = Field(ge=0)
    hours_by_kind: dict[str, int] = Field(default_factory=dict)
    control_forms: list[str] = Field(default_factory=list)

class PracticeStage(ContractModel):
    source_ref: SourceRef
    name: str
    description: str
    semester: int | None = Field(default=None, ge=1, le=8)
    hours: int | None = Field(default=None, ge=0)

class MatrixCell(ContractModel):
    source_ref: SourceRef
    discipline_name: str
    semester: int | None = Field(default=None, ge=1, le=8)
    competency_code: str
    value: Literal[0,1]

class ProfessionalRole(ContractModel):
    source_ref: SourceRef
    local_id: str
    name: str

class RoleCompetencyLink(ContractModel):
    source_ref: SourceRef
    role_id: str
    competency_code: str

class IndicatorOutcomeLink(ContractModel):
    source_ref: SourceRef
    indicator_code: str
    learning_outcome_id: str

class ProgramV2(Program):
    profile: str | None = None
    study_duration: str | None = None
    study_form: str | None = None
    education_languages: list[str] = Field(default_factory=list)
    program_cipher: str | None = None

class AssessmentToolV2(AssessmentTool):
    description: str | None = None
    task_examples: list[str] = Field(default_factory=list)

class WorkingProgramOutput(ContractModel):
    education_languages: list[str] = Field(default_factory=list)
    education_languages_source_ref: SourceRef | None = None
    title_credit_units: int | None = Field(default=None, ge=1)
    title_credit_units_source_ref: SourceRef | None = None


class RPDOutput(WorkingProgramOutput):
    meta: RPDMeta
    disciplines: list[Discipline] = Field(min_length=1)
    competencies: list[Competency] = Field(default_factory=list)
    indicators: list[Indicator] = Field(default_factory=list)
    learning_outcomes: list[LearningOutcome] = Field(default_factory=list)
    assessment_tools: list[AssessmentToolV2] = Field(default_factory=list)
    course_content: list[CourseTopic] = Field(default_factory=list)
    document_sections: list[DocumentSection] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    score_scale: list[ScoreScaleItem] = Field(default_factory=list)
    parse_issues: list[ParseIssue] = Field(default_factory=list)

class RPPOutput(WorkingProgramOutput):
    meta: RPPMeta
    disciplines: list[Discipline] = Field(min_length=1)
    semester_workload: list[SemesterWorkload]
    document_sections: list[DocumentSection] = Field(default_factory=list)
    practice_stages: list[PracticeStage] = Field(default_factory=list)
    competencies: list[Competency] = Field(default_factory=list)
    indicators: list[Indicator] = Field(default_factory=list)
    learning_outcomes: list[LearningOutcome] = Field(default_factory=list)
    assessment_tools: list[AssessmentToolV2] = Field(default_factory=list)
    course_content: list[CourseTopic] = Field(default_factory=list)
    score_scale: list[ScoreScaleItem] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    parse_issues: list[ParseIssue] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_workload(self):
        # Числовая целостность является бизнес-проверкой: здесь проверяем лишь
        # уникальность семестров и наличие их в родительской записи.
        semesters = [w.semester for w in self.semester_workload]
        if len(semesters) != len(set(semesters)):
            raise ValueError('Дубли семестров в semester_workload')
        if not set(semesters).issubset(set(self.disciplines[0].semesters)):
            raise ValueError('Семестр нагрузки отсутствует в Discipline.semesters')
        return self

class CurriculumOutput(ContractModel):
    meta: CurriculumMeta
    competencies: list[Competency] = Field(default_factory=list)
    curriculum_items: list[CurriculumItem] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    parse_issues: list[ParseIssue] = Field(default_factory=list)

class MatrixOutput(ContractModel):
    meta: MatrixMeta
    matrix_cells: list[MatrixCell] = Field(min_length=1)
    competencies: list[Competency] = Field(default_factory=list)
    parse_issues: list[ParseIssue] = Field(default_factory=list)

class CKMVOutput(ContractModel):
    meta: CKMVMeta
    competencies: list[Competency] = Field(default_factory=list)
    indicators: list[Indicator] = Field(default_factory=list)
    learning_outcomes: list[LearningOutcome] = Field(default_factory=list)
    professional_roles: list[ProfessionalRole] = Field(default_factory=list)
    role_competency_links: list[RoleCompetencyLink] = Field(default_factory=list)
    indicator_outcome_links: list[IndicatorOutcomeLink] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    parse_issues: list[ParseIssue] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_links(self):
        competencies={c.code for c in self.competencies}
        indicators={i.code for i in self.indicators}
        outcomes={o.local_id for o in self.learning_outcomes}
        roles={r.local_id for r in self.professional_roles}
        for item in self.indicators:
            if item.competency_code not in competencies:
                raise ValueError(f'Индикатор {item.code} без компетенции')
        for link in self.role_competency_links:
            if link.role_id not in roles or link.competency_code not in competencies:
                raise ValueError('Неизвестная роль/компетенция в связи')
        for link in self.indicator_outcome_links:
            if link.indicator_code not in indicators or link.learning_outcome_id not in outcomes:
                raise ValueError('Неизвестный индикатор/результат в связи')
        return self

class CharacteristicOutput(ContractModel):
    meta: CharacteristicMeta
    program: ProgramV2
    competencies: list[Competency] = Field(default_factory=list)
    parse_issues: list[ParseIssue] = Field(default_factory=list)

DocumentOutput: TypeAlias = (
    RPDOutput
    | RPPOutput
    | CurriculumOutput
    | MatrixOutput
    | CKMVOutput
    | CharacteristicOutput
)
MODELS = {'RPD': RPDOutput,'RPP': RPPOutput,'CURRICULUM': CurriculumOutput,'MATRIX': MatrixOutput,'CKMV': CKMVOutput,'CHARACTERISTIC': CharacteristicOutput}
def parse_document(payload: dict) -> DocumentOutput:
    kind = payload.get('meta',{}).get('doc_type')
    if kind not in MODELS:
        raise ValueError(f'Неизвестный тип документа: {kind!r}')
    return MODELS[kind].model_validate(payload)
