"""Контракт выхода парсера: один JSON на файл.

Четыре части: шапка (meta), сущности с обязательным source_ref, связи
и проблемы разбора (parse_issues).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DocType = Literal[
    "RPD",            # рабочая программа дисциплины
    "RPP",            # рабочая программа практики
    "CURRICULUM",     # учебный план (xlsx)
    "MATRIX",         # матрица компетенций (xlsx)
    "CHARACTERISTIC", # общая характеристика программы
    "CKMV",           # ЦКМВ
]
Severity = Literal["error", "warning", "info"]
IssueCategory = Literal[
    "not_found",         # ожидаемый раздел/таблица не найдены
    "unparsed_row",      # строка не распознана конфигом
    "empty_cell",        # пустая обязательная ячейка
    "unexpected_format", # структура отличается от конфига шаблона
    "ambiguous",         # значение допускает несколько прочтений
    "manual_review",     # конфиг не покрыл поле — кандидат на LLM-fallback
]
EntityName = Literal[
    "program", "discipline", "competency", "indicator", "learning_outcome",
    "assessment_tool", "curriculum_item", "course_topic",
]
RelationKind = Literal[
    "формирует",   # дисциплина → компетенция
    "имеет",       # компетенция → индикатор
    "проверяется", # индикатор → оценочное средство
    "включает",    # компетенция → результат обучения
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(ContractModel):
    """Место в исходном документе: file обязателен, остальное — что применимо.

    row/col нумеруются с 1; row=1 — шапка таблицы.
    """

    file: str
    section: Optional[str] = None
    table: Optional[int] = None
    row: Optional[int] = None
    col: Optional[int] = None

    @field_validator("file")
    @classmethod
    def _file_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_ref.file пуст")
        return v


class BaseEntity(ContractModel):
    """Общая модель для извлечённых сущностей с указанием источника."""

    source_ref: SourceRef


class Program(BaseEntity):
    name: Optional[str] = None
    level: Optional[Literal["бакалавриат", "магистратура", "специалитет", "аспирантура"]] = None
    direction_code: Optional[str] = None         # «38.03.05»
    education_standard: Optional[str] = None     # «ОС СПбГУ BM.5829/23» или «ФГОС 38.03.05»
    professional_standards: list[str] = []       # профкоды из Характеристики


class Discipline(BaseEntity):
    name: str
    semesters: list[int]
    credit_units: int = Field(ge=1)              # ЗЕТ; 
    hours_total: int = Field(ge=0)
    hours_by_kind: dict[str, int]                # «лекции»: 62, «самостоятельная работа»: 82…
    control_forms: list[str] = []                # «зачёт», «экзамен»
    prerequisites: list[str] = []                # названия дисциплин из раздела 1.2

    @field_validator("semesters")
    @classmethod
    def _semesters_1_8(cls, v: list[int]) -> list[int]:
        bad = [s for s in v if not 1 <= s <= 8]
        if bad:
            raise ValueError(f"семестры вне 1–8: {bad}")
        return v


class Competency(BaseEntity):
    code: str                                    # УК-1, ОПК-1, ПКП-УНК-03, ПКА-1…
    formulation: str

    @field_validator("code")
    @classmethod
    def _code_no_spaces(cls, v: str) -> str:
        if not v.strip() or len(v.split()) > 1:
            raise ValueError("код компетенции пуст или содержит пробелы")
        return v


class Indicator(BaseEntity):
    code: str                                    # УК-1.1, ПКП-УНК-03.01…
    text: str
    competency_code: str                         # родительская компетенция в этом же файле


class LearningOutcome(BaseEntity):
    """Результат обучения «знать/уметь/владеть».

    local_id назначает парсер (уникален в файле, «LO-001») — ключ для связей:
    в источнике у результатов кода нет, кроме вариантов вида «ПКП-УНК-03.01. У-1».
    kind=None — тип результата не удалось определить по данным источника.
    """

    local_id: str
    text: str
    kind: Optional[Literal["знать", "уметь", "владеть"]] = None
    code: Optional[str] = None

    @field_validator("local_id")
    @classmethod
    def _id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("local_id пуст")
        return v


class AssessmentTool(BaseEntity):
    name: str                                    # «Контрольные работы», «Вопросы к экзамену»…


class CurriculumItem(BaseEntity):
    """Строка учебного плана (xlsx)."""

    discipline_name: str
    semester: int = Field(ge=1, le=8)
    credit_units: int = Field(ge=1)
    hours_total: int = Field(ge=0)
    block: str                                   # «Блок 1», «Блок 3.1 — вариативная часть»…


class CourseTopic(BaseEntity):
    """Строка таблицы «Структура и содержание»: тема × вид занятий × часы."""

    topic: str
    activity: str                                # «лекции», «практические занятия», «по методическим материалам»…
    hours: int = Field(ge=0)
    module: Optional[str] = None                 # «Модуль 1. Очерк теории чисел»


class ScoreScaleItem(BaseEntity):
    """Балльная шкала: оценка СПбГУ × ECTS × критерии."""

    grade_spbu: str
    grade_ects: Optional[str] = None
    points: Optional[str] = None                 
    criterion: Optional[str] = None


class Relation(BaseEntity):
    """Связь между сущностями. Ссылка на сущность — по естественному ключу:
    discipline/curriculum_item/course_topic — название, остальные — код.
    """

    src_entity: EntityName
    src_key: str
    dst_entity: EntityName
    dst_key: str
    kind: RelationKind

    @field_validator("src_key", "dst_key")
    @classmethod
    def _key_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("ключ связи пуст")
        return v


class ParseIssue(ContractModel):

    category: IssueCategory
    message: str
    severity: Severity = "warning"
    source_ref: Optional[SourceRef] = None


class ParserMeta(ContractModel):
    schema_version: Literal[1] = 1
    doc_type: DocType
    file_name: str
    file_hash: str                               # sha256 исходного файла
    parser_config: str                           # «rpd_spbu_v1» — каким конфигом извлекали
    program_id: Optional[str] = None             # назначает конвейер при загрузке пакета

    @field_validator("file_hash")
    @classmethod
    def _hash_sha256(cls, v: str) -> str:
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v.lower()):
            raise ValueError("file_hash должен быть sha256 (64 hex-символа)")
        return v.lower()


class ParserOutput(ContractModel):
    """Весь JSON, который парсер отдаёт на один входной файл."""

    meta: ParserMeta
    program: Optional[Program] = None
    disciplines: list[Discipline] = []
    competencies: list[Competency] = []
    indicators: list[Indicator] = []
    learning_outcomes: list[LearningOutcome] = []
    assessment_tools: list[AssessmentTool] = []
    curriculum_items: list[CurriculumItem] = []
    course_content: list[CourseTopic] = []
    score_scale: list[ScoreScaleItem] = []
    relations: list[Relation] = []
    parse_issues: list[ParseIssue] = []

    @model_validator(mode="after")
    def _indicator_parent_in_file(self) -> "ParserOutput":
        known = {c.code for c in self.competencies}
        for ind in self.indicators:
            if ind.competency_code not in known:
                raise ValueError(
                    f"индикатор {ind.code}: компетенция {ind.competency_code} "
                    "не извлечена в этом же файле (сирота)"
                )
        return self
