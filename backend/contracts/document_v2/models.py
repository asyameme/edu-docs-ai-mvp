from __future__ import annotations
from typing import Literal, Union
from pydantic import Field, model_validator
from backend.contracts.parser_output import (
    ContractModel,
    SourceRef as SourceRefV1,
    ParseIssue,
    Competency as CompetencyV1,
    Indicator as IndicatorV1,
    LearningOutcome as LearningOutcomeV1,
    AssessmentTool as AssessmentToolV1,
    CurriculumItem as CurriculumItemV1,
    CourseTopic as CourseTopicV1,
    ScoreScaleItem as ScoreScaleItemV1,
    Relation as RelationV1,
    Program as ProgramV1,
    Discipline as DisciplineV1,
    ParserMeta,
)


class SourceRefV2(SourceRefV1):
    paragraph_index: int | None = Field(default=None, ge=0)
    sheet_name: str | None = None
    cell: str | None = None


SourceRef = SourceRefV2


class Competency(CompetencyV1):
    source_ref: SourceRefV2


class Indicator(IndicatorV1):
    source_ref: SourceRefV2


class LearningOutcome(LearningOutcomeV1):
    source_ref: SourceRefV2


class AssessmentTool(AssessmentToolV1):
    source_ref: SourceRefV2


class CurriculumItem(CurriculumItemV1):
    source_ref: SourceRefV2


class CourseTopic(CourseTopicV1):
    source_ref: SourceRefV2


class ScoreScaleItem(ScoreScaleItemV1):
    source_ref: SourceRefV2


class Relation(RelationV1):
    source_ref: SourceRefV2


class Program(ProgramV1):
    source_ref: SourceRefV2


class Discipline(DisciplineV1):
    source_ref: SourceRefV2


class MetaV2(ParserMeta):
    schema_version: Literal[2] = 2

class RPDMeta(MetaV2):
    doc_type: Literal['RPD'] = 'RPD'
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

class RPDOutput(ContractModel):
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

class RPPOutput(ContractModel):
    meta: RPPMeta
    disciplines: list[Discipline] = Field(min_length=1)
    semester_workload: list[SemesterWorkload] = Field(min_length=1)
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

DocumentOutput = Annotated[Union[RPDOutput,RPPOutput,CurriculumOutput,MatrixOutput,CKMVOutput,CharacteristicOutput], Field(discriminator='meta.doc_type')] if False else Union[RPDOutput,RPPOutput,CurriculumOutput,MatrixOutput,CKMVOutput,CharacteristicOutput]
MODELS = {'RPD': RPDOutput,'RPP': RPPOutput,'CURRICULUM': CurriculumOutput,'MATRIX': MatrixOutput,'CKMV': CKMVOutput,'CHARACTERISTIC': CharacteristicOutput}
def parse_document(payload: dict) -> DocumentOutput:
    kind = payload.get('meta',{}).get('doc_type')
    if kind not in MODELS:
        raise ValueError(f'Неизвестный тип документа: {kind!r}')
    return MODELS[kind].model_validate(payload)
