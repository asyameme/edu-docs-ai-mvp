import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { ImportProgress } from "./ImportProgress";
import { GenerationScreen } from "./GenerationScreen";
import { DiagnosticList } from "./DiagnosticList";
import { groupDiagnostics, importDiagnostics, reviewDiagnostic, ruleDisplayLabel, updateAcceptedWarnings, type Diagnostic } from "./diagnostics";
import { useImportJob } from "./useImportJob";
import { api } from "./api";
import type {
  ChatOutput,
  ChatProposedChange,
  ChatSessionHistoryRead,
  ChatSessionRead,
  ChatTurnHistoryRead,
  ImportJob,
  OptionalRuleCode,
  ReviewDetails,
  ReviewIssue,
  RuleCoverage,
} from "./api";

type ProgramDocument = {
  name: string;
  status: string;
};

type FindingChoice = {
  id: string;
  value: string;
  documents: string[];
  documentCount: number;
};

type ReconciliationFinding = {
  id: string;
  title: string;
  description: string;
  choices: FindingChoice[];
  selectedChoiceId?: string;
  ruleCode?: string;
};

function coverageStatusLabel(status: RuleCoverage["status"]): string {
  switch (status) {
    case "passed": return "Пройдена";
    case "failed": return "Есть замечания";
    case "needs_review": return "Нужна ручная проверка";
    case "skipped": return "Пропущена";
    case "not_applicable": return "Не применяется";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

const optionalRuleOptions: Array<{ code: OptionalRuleCode; title: string; description: string }> = [
  {
    code: "R-09",
    title: "Покрытие компетенций учебным планом",
    description: "Смысловая оценка покрытия компетенций по дисциплинам и результатам обучения.",
  },
  {
    code: "R-11",
    title: "Результаты обучения и содержание РПД",
    description: "Сопоставление результатов обучения с содержанием раздела 2 и оцениванием раздела 3.",
  },
];

type ParsedProgram = {
  documentCount: number;
  findings: ReconciliationFinding[];
  issues: Diagnostic[];
};

type ChatPlan =
  | {
    kind: "demo";
    id: string;
    title: string;
    changes: string[];
    status: "pending" | "applied" | "cancelled";
    validation?: string[];
  }
  | {
    kind: "proposal";
    turnId: string;
    expectedVersionId: string;
    title: string;
    changes: ChatProposedChange[];
    unresolvedQuestions: string[];
    status: "pending" | "applied" | "cancelled";
  };

type SourceCoordinate =
  | { kind: "paragraph"; paragraphIndex: number }
  | { kind: "table_cell"; tableIndex: number; rowIndex: number; columnIndex: number };

type SourceRef = {
  id: string;
  documentId: string;
  documentVersionId: string;
  fileName: string;
  versionLabel: string;
  coordinate: SourceCoordinate;
};

type SourceFragment = {
  sourceRef: SourceRef;
  excerpt: string;
  context: Array<{ relation: "before" | "after"; text: string }>;
  tableRow?: { headers: string[]; cells: string[] };
};

type ChatMessage = {
  id: string;
  role: "agent" | "user";
  text: string;
  result?: {
    kind: "coverage";
    title: string;
    status: string;
    sourceLabel: string;
    sourceRef: SourceRef;
  };
  entity?: {
    name: string;
    details: string[];
    sourceRef: SourceRef;
  };
  plan?: ChatPlan;
  questions?: string[];
  sourceLabels?: string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function parseProposedChange(value: unknown): ChatProposedChange | null {
  if (!isRecord(value)
    || (value.target_kind !== "entity" && value.target_kind !== "relation")
    || typeof value.target_id !== "string" || typeof value.field_path !== "string"
    || !Object.hasOwn(value, "expected_old_value") || !Object.hasOwn(value, "new_value")
    || typeof value.reason !== "string" || !isStringArray(value.source_ref_ids)) return null;
  return {
    target_kind: value.target_kind,
    target_id: value.target_id,
    field_path: value.field_path,
    expected_old_value: value.expected_old_value,
    new_value: value.new_value,
    reason: value.reason,
    source_ref_ids: value.source_ref_ids,
  };
}

function parseChatOutput(value: unknown): ChatOutput | null {
  if (!isRecord(value)) return null;
  if (value.kind === "answer" && typeof value.message === "string" && Array.isArray(value.sources)) {
    const sourceValues: unknown[] = value.sources;
    const sources = sourceValues.flatMap((source) => (
      isRecord(source) && typeof source.id === "string" ? [{ id: source.id }] : []
    ));
    if (sources.length !== sourceValues.length) return null;
    return {
      kind: "answer", message: value.message, sources,
    };
  }
  if (value.kind === "clarification" && typeof value.message === "string" && isStringArray(value.questions)) {
    return { kind: "clarification", message: value.message, questions: value.questions };
  }
  if (value.kind === "change_proposal" && typeof value.summary === "string"
    && isStringArray(value.unresolved_questions) && Array.isArray(value.changes)) {
    const rawChanges: unknown[] = value.changes;
    const changes = rawChanges.flatMap((change) => {
      const parsed = parseProposedChange(change);
      return parsed ? [parsed] : [];
    });
    if (changes.length !== rawChanges.length) return null;
    return {
      kind: "change_proposal",
      summary: value.summary,
      unresolved_questions: value.unresolved_questions,
      changes,
    };
  }
  return null;
}

function waitForChatOutput(programId: string, runId: string): Promise<ChatOutput> {
  return new Promise((resolve, reject) => {
    const stream = new EventSource(`/api/v1/programs/${programId}/chat/runs/${runId}/events`);
    let timeout = 0;
    const finish = (error: Error | null, output?: ChatOutput) => {
      window.clearTimeout(timeout);
      stream.close();
      if (error) reject(error);
      else if (output) resolve(output);
      else reject(new Error("Агент вернул пустой ответ."));
    };
    stream.addEventListener("completed", (event) => {
      if (!(event instanceof MessageEvent)) {
        finish(new Error("Не удалось прочитать ответ агента."));
        return;
      }
      try {
        const parsed: unknown = JSON.parse(event.data);
        const output = parseChatOutput(parsed);
        finish(output ? null : new Error("Формат ответа агента не распознан."), output ?? undefined);
      } catch {
        finish(new Error("Не удалось разобрать ответ агента."));
      }
    });
    stream.addEventListener("failed", () => finish(new Error("Агент не смог выполнить запрос. Попробуйте ещё раз.")));
    stream.addEventListener("error", () => {
      if (stream.readyState === EventSource.CLOSED) finish(new Error("Соединение с чатом потеряно."));
    });
    timeout = window.setTimeout(() => finish(new Error("Агент не ответил за отведённое время.")), 5 * 60 * 1000);
  });
}

function chatSourceLabels(ids: string[], reviewDetails: ReviewDetails | null, job: ImportJob | null) {
  const sourceRefs = new Map((reviewDetails?.version.snapshot.source_refs ?? []).map((source) => [source.id, source]));
  const documentNames = new Map((job?.files ?? []).map((file) => [file.document_id, file.member_name]));
  return ids.map((id) => {
    const source = sourceRefs.get(id);
    if (!source) return null;
    const fileName = documentNames.get(source.document_id) ?? "Документ пакета";
    return `${shortDocumentName(fileName)}${source.section ? ` · ${source.section}` : ""}`;
  }).filter((label): label is string => label !== null);
}

function chatOutputMessage(
  turnId: string,
  result: ChatOutput,
  expectedVersionId: string,
  reviewDetails: ReviewDetails | null,
  job: ImportJob | null,
  accepted = false,
): ChatMessage {
  if (result.kind === "answer") {
    return {
      id: `${turnId}-agent`, role: "agent", text: result.message,
      sourceLabels: chatSourceLabels(result.sources.map((source) => source.id), reviewDetails, job),
    };
  }
  if (result.kind === "clarification") {
    return { id: `${turnId}-agent`, role: "agent", text: result.message, questions: result.questions };
  }
  return {
    id: `${turnId}-agent`, role: "agent",
    text: "Предложение подготовлено. Проверьте изменения перед применением.",
    plan: {
      kind: "proposal", turnId, expectedVersionId,
      title: result.summary, changes: result.changes,
      unresolvedQuestions: result.unresolved_questions,
      status: accepted ? "applied" : "pending",
    },
  };
}

function chatHistoryMessages(
  history: ChatSessionHistoryRead,
  reviewDetails: ReviewDetails | null,
  job: ImportJob | null,
): ChatMessage[] {
  const acceptedTurns = new Set((reviewDetails?.version.snapshot.review?.decisions ?? []).map((decision) => decision.conflict_id));

  return history.turns.flatMap((turn: ChatTurnHistoryRead) => {
    const userMessage: ChatMessage = { id: `${turn.id}-user`, role: "user", text: turn.user_text };
    if (turn.status === "pending") {
      return [userMessage, { id: `${turn.id}-agent`, role: "agent", text: "Обрабатываю запрос…" }];
    }
    if (turn.status === "failed" || !turn.result) {
      return [userMessage, { id: `${turn.id}-agent`, role: "agent", text: "Запрос не выполнен. Можно отправить его ещё раз." }];
    }
    const result = turn.result;
    const agentMessage = chatOutputMessage(
      turn.id, result, history.version_id, reviewDetails, job, acceptedTurns.has(turn.id),
    );
    return [userMessage, agentMessage];
  });
}

type AppScreen = "Программа" | "Чат" | "Генерация";

const demoProgram: ParsedProgram = {
  documentCount: 12,
  findings: [
    {
      id: "competence-code",
      ruleCode: "R-01",
      title: "Код компетенции различается",
      description: "План: УК-2 · РПД: УК-3 | Учебный план, стр. 8 · РПД, разд. 3.2",
      choices: [
        {
          id: "study-plan",
          value: "Оставить УК-2",
          documents: ["Учебный план.docx"],
          documentCount: 1,
        },
        {
          id: "rpd",
          value: "Оставить УК-3",
          documents: ["РПД_Анализ_данных.docx"],
          documentCount: 1,
        },
        {
          id: "both",
          value: "Оба варианта",
          documents: ["Учебный план.docx", "РПД_Анализ_данных.docx"],
          documentCount: 2,
        },
      ],
    },
  ],
  issues: [{
    id: "demo-issue", origin: "validation", severity: "error", category: "indicator_missing",
    ruleCode: "R-04", title: "Для компетенции не найден индикатор достижения",
    message: "Для компетенции не найден индикатор достижения",
    documents: [{ id: "demo-rpd", name: "РПД_Анализ_данных.docx" }], detail: null,
  }, {
    id: "issue-2", origin: "validation", severity: "warning", category: "outcome_formulation_mismatch",
    ruleCode: "R-11", title: "Формулировка результата обучения отличается от учебного плана",
    message: "Формулировка результата обучения отличается от учебного плана",
    documents: [{ id: "demo-plan", name: "Учебный план.docx" }], detail: null,
  }, {
    id: "issue-3", origin: "validation", severity: "warning", category: "outcome_formulation_mismatch",
    ruleCode: "R-11", title: "В документах различаются формулировки результатов обучения",
    message: "В документах различаются формулировки результатов обучения",
    documents: [{ id: "demo-plan", name: "Учебный план.docx" }], detail: null,
  }],
};

const navigation = ["Программа", "Чат", "Генерация"] as const;

const demoMLOpsSourceRef: SourceRef = {
  id: "demo-mlops-rpd-v1-paragraph-42",
  documentId: "demo-mlops-rpd",
  documentVersionId: "demo-rpd-v1",
  fileName: "РПД MLOps.docx",
  versionLabel: "v1",
  coordinate: { kind: "paragraph", paragraphIndex: 42 },
};

const demoRpdSourceRef: SourceRef = {
  id: "demo-rpd-v1-paragraph-42",
  documentId: "demo-rpd",
  documentVersionId: "demo-rpd-v1",
  fileName: "РПД_Анализ_данных.docx",
  versionLabel: "v1",
  coordinate: { kind: "paragraph", paragraphIndex: 42 },
};

const demoChatMessages: ChatMessage[] = [
  {
    id: "welcome",
    role: "agent",
    text: "Документы программы обработаны. Задайте вопрос или опишите, что хотите изменить.",
  },
  {
    id: "sample-request",
    role: "user",
    text: "Проверь покрытие результатов обучения в MLOps",
  },
  {
    id: "coverage-result",
    role: "agent",
    text: "Для результата обучения РО-2 не найдено подтверждение в программе курса.",
    result: {
      kind: "coverage",
      title: "Покрытие частичное",
      status: "Требует внимания",
      sourceLabel: "РПД MLOps · раздел 4.2",
      sourceRef: demoMLOpsSourceRef,
    },
  },
  {
    id: "assessment-request",
    role: "user",
    text: "Хочу изменить способ оценки в дисциплине «Технологии анализа данных».",
  },
  {
    id: "found-entity",
    role: "agent",
    text: "Нашёл дисциплину. Вот текущие данные и источник в пакете документов:",
    entity: {
      name: "Технологии анализа данных",
      details: ["Максимум за дисциплину — 100 баллов", "В распределении участвуют 2 формы оценивания"],
      sourceRef: demoRpdSourceRef,
    },
  },
  {
    id: "sample-plan",
    role: "agent",
    text: "Предлагаю такой план правок. Проверьте изменения перед применением:",
    plan: {
      kind: "demo",
      id: "assessment-plan",
      title: "Изменить распределение баллов",
      changes: [
        "Проект: 60 → 50 баллов",
        "Практические работы: 40 → 50 баллов",
      ],
      status: "pending",
    },
  },
];

// Demo SourceFragment response is kept separately from the source_ref pointer.
const demoSourceFragments: Record<string, SourceFragment> = {
  [demoMLOpsSourceRef.id]: {
    sourceRef: demoMLOpsSourceRef,
    excerpt: "Для результата обучения РО-2 подтверждение в программе курса не найдено.",
    context: [
      { relation: "before", text: "4. Результаты обучения и их связь с содержанием дисциплины" },
      { relation: "after", text: "Проверьте связь результата обучения с разделами программы курса." },
    ],
  },
  [demoRpdSourceRef.id]: {
    sourceRef: demoRpdSourceRef,
    excerpt: "Формы текущего контроля: практические работы — 40 баллов; проект — 60 баллов.",
    context: [
      { relation: "before", text: "5. Формы текущего контроля и промежуточной аттестации" },
      { relation: "after", text: "Сумма баллов по дисциплине составляет 100." },
    ],
  },
};

function formatSourceLocation(sourceRef: SourceRef) {
  const { coordinate } = sourceRef;
  if (coordinate.kind === "paragraph") return `абзац ${coordinate.paragraphIndex}`;
  return `таблица ${coordinate.tableIndex}, строка ${coordinate.rowIndex}, столбец ${coordinate.columnIndex}`;
}

function isSupportedFile(file: File) {
  return /\.zip$/i.test(file.name);
}

function formatFileCount(count: number) {
  return formatCountWithWords(count, "файл", "файла", "файлов");
}

function formatCountWithWords(count: number, one: string, few: string, many: string) {
  const lastTwoDigits = count % 100;
  const lastDigit = count % 10;
  if (lastTwoDigits >= 11 && lastTwoDigits <= 14) return `${count} ${many}`;
  if (lastDigit === 1) return `${count} ${one}`;
  if (lastDigit >= 2 && lastDigit <= 4) return `${count} ${few}`;
  return `${count} ${many}`;
}

function shortDocumentName(name: string) {
  return name.replaceAll("\\", "/").split("/").pop() || name;
}

function shortChoiceLabel(label: string) {
  return label.replace(/^Выбрать значение из документов:\s*/i, "").trim();
}

function formatIssueSummary(issues: Diagnostic[]) {
  const groups = groupDiagnostics(issues);
  const actionable = groups.filter((group) => group.severity !== "info").length;
  const information = groups.length - actionable;
  return [
    actionable ? `Групп замечаний: ${actionable}` : "Ошибок и предупреждений нет",
    information ? `Информация: ${information}` : "",
  ].filter(Boolean).join(" · ");
}

type FindingFilter = "all" | "R-01" | "R-07" | "R-08" | "R-11";
type FindingStatusFilter = "all" | "pending" | "saved";

function findingRuleGroup(ruleCode?: string) {
  return ruleCode?.match(/^R-(01|07|08|11)(?:$|[^0-9])/)?.[0]?.slice(0, 4) ?? "other";
}

function issueRuleGroup(ruleId?: string) {
  return ruleId?.match(/^R-(01|07|08|11)(?:$|[^0-9])/)?.[0]?.slice(0, 4) ?? "other";
}

// Подсветка только визуальная: показывает отличающиеся слова, а не вывод о смысловой эквивалентности.
function ComparisonText({ value, otherValues }: { value: string; otherValues: string[] }) {
  const pieces = value.match(/\s+|[^\s]+/g) ?? [];
  const otherWords = otherValues.map((text) => new Set((text.match(/[^\s]+/g) ?? [])
    .map((word) => word.toLocaleLowerCase("ru").replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, ""))));
  return <span>{pieces.map((piece, index) => {
    const word = piece.toLocaleLowerCase("ru").replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
    const differs = Boolean(word) && otherWords.some((words) => !words.has(word));
    return differs
      ? <mark className="competency-diff-word" key={index}>{piece}</mark>
      : <span key={index}>{piece}</span>;
  })}</span>;
}

function mapReviewToProgram(details: ReviewDetails, job: ImportJob | null): ParsedProgram {
  const snapshot = details.version.snapshot;
  const sourceRefs = new Map(snapshot.source_refs.map((source) => [source.id, source]));
  const documentNames = new Map((job?.files ?? []).map((file) => [file.document_id, file.member_name]));
  const documentCount = job?.files.length ?? snapshot.source_refs.length;
  const sourceDescription = (
    ids: string[],
    documentIds: string[] = [],
  ) => {
    const sources = ids.map((id) => sourceRefs.get(id)).filter((source) => source !== undefined);
    const sourceDocumentIds = [...new Set([
      ...documentIds,
      ...sources.map((source) => source.document_id),
    ])];
    const names = [...new Set(
      sourceDocumentIds
        .map((id) => documentNames.get(id))
        .filter((name) => name !== undefined)
        .map(shortDocumentName),
    )];
    return {
      source: names.length ? names.join(" · ") : "Источник пакета документов",
      documents: names,
      documentCount: sourceDocumentIds.length,
      diagnosticDocuments: sourceDocumentIds.map((id) => ({
        id, name: documentNames.get(id) ?? "Документ пакета",
      })),
    };
  };
  const findings = details.issues.filter((issue) => issue.requires_decision)
    .map((issue) => {
      const choices = issue.variants.map((variant) => {
        const changeSourceIds = variant.changes.flatMap((change) => [
          ...("entity" in change ? change.entity.source_ref_ids : []),
          ...("relation" in change ? change.relation.source_ref_ids : []),
        ]);
        const changeDocumentIds = variant.changes.flatMap((change) =>
          change.op === "set_document_value" ? [change.document_id] : [],
        );
        const source = sourceDescription(
          changeSourceIds.length ? changeSourceIds : issue.source_ref_ids,
          changeDocumentIds.length ? changeDocumentIds : issue.source_document_ids ?? [],
        );
        return { id: variant.id, value: shortChoiceLabel(variant.label), documents: source.documents, documentCount: source.documentCount };
      });
      const choiceSummary = choices.map((choice) => choice.value).join(" · ");
      return {
        id: issue.id,
        ruleCode: issue.rule_code,
        title: issue.message,
        description: choiceSummary || `Правило ${issue.rule_code} · ${issue.type}`,
        selectedChoiceId: issue.decision?.variant_id,
        choices,
      };
    });
  const diagnosticIssues = details.check_report
    ? [...details.check_report.issues, ...details.issues.filter((issue) => issue.rule_code.startsWith("BACKEND_GRAPH_"))]
    : details.issues;
  const reviewIssues = diagnosticIssues.filter((issue) => !issue.requires_decision).map((issue) => {
    const source = sourceDescription(issue.source_ref_ids, issue.source_document_ids ?? []);
    return reviewDiagnostic(issue, source.diagnosticDocuments);
  });
  const importIssues = job ? importDiagnostics(job) : [];

  return {
    documentCount,
    findings,
    issues: [...reviewIssues, ...importIssues],
  };
}

type ChatScreenProps = {
  versionNumber: number;
  currentVersionId: string;
  isDemoMode: boolean;
  isConfirmed: boolean;
  hasSignature: boolean;
  canUseChat: boolean;
  isBusy: boolean;
  statusMessage: string;
  messages: ChatMessage[];
  draft: string;
  selectedSourceRef: SourceRef | null;
  onDraftChange: (value: string) => void;
  onSend: (message?: string) => void | Promise<void>;
  onApplyPlan: (messageId: string) => void | Promise<void>;
  onCancelPlan: (messageId: string) => void;
  onOpenSource: (sourceRef: SourceRef) => void;
  onCloseSource: () => void;
};

function formatChatValue(value: unknown) {
  if (typeof value === "string") return value;
  if (value === null) return "пусто";
  return JSON.stringify(value) ?? "пусто";
}

function ChatScreen({
  versionNumber,
  currentVersionId,
  isDemoMode,
  isConfirmed,
  hasSignature,
  canUseChat,
  isBusy,
  statusMessage,
  messages,
  draft,
  selectedSourceRef,
  onDraftChange,
  onSend,
  onApplyPlan,
  onCancelPlan,
  onOpenSource,
  onCloseSource,
}: ChatScreenProps) {
  return (
    <>
      <section className="page-heading">
        <div>
          <h1>Чат с агентом</h1>
          <p className="eyebrow">Вопросы, проверки и изменения программы</p>
        </div>
        <span className={`status-pill ${isDemoMode || isConfirmed ? "status-pill--green" : "status-pill--muted"}`}>
          Версия {versionNumber}
        </span>
      </section>

      <section className="chat-panel card" aria-label="Чат с агентом">
        <div className="chat-transcript" aria-live="polite" aria-relevant="additions text">
          {messages.length === 0 ? (
            <div className="chat-empty-state">
              <div className="agent-mark" aria-hidden="true">ИИ</div>
              <h2>{canUseChat ? "Начните диалог с агентом" : "Чат пока недоступен"}</h2>
              <p>
                {canUseChat
                  ? "Задайте вопрос по документам или опишите нужное изменение."
                  : statusMessage}
              </p>
            </div>
          ) : (
            messages.map((message) => (
              <article className={`chat-message chat-message--${message.role}`} key={message.id}>
                {message.role === "agent" && <div className="agent-mark" aria-hidden="true">ИИ</div>}
                <div className="message-column">
                  <div className="message-meta">{message.role === "agent" ? "Агент" : "Вы"}</div>
                  <div className={`message-bubble message-bubble--${message.role}`}>
                    {message.result?.kind === "coverage" && (
                      <strong className="message-result-title">{message.result.title}</strong>
                    )}
                    {message.text && <p className="message-body">{message.text}</p>}
                    {message.questions && <ul className="chat-clarification-questions">
                      {message.questions.map((question) => <li key={question}>{question}</li>)}
                    </ul>}
                    {message.sourceLabels && message.sourceLabels.length > 0 && (
                      <p className="chat-source-labels">Источники: {message.sourceLabels.join(" · ")}</p>
                    )}
                    {message.result?.kind === "coverage" && (
                      <>
                        <span className="message-result-status">{message.result.status}</span>
                        <button
                          type="button"
                          className="entity-source message-source"
                          aria-haspopup="dialog"
                          aria-expanded={selectedSourceRef?.id === message.result.sourceRef.id}
                          onClick={() => {
                            if (message.result?.kind === "coverage") onOpenSource(message.result.sourceRef);
                          }}
                        >
                          Источник: {message.result.sourceLabel}
                        </button>
                      </>
                    )}
                  </div>

                  {message.entity && (
                    <section className="entity-card" aria-label="Найденная сущность">
                      <div className="entity-card-label">Найденная дисциплина</div>
                      <h3>{message.entity.name}</h3>
                      <ul>
                        {message.entity.details.map((detail) => <li key={detail}>{detail}</li>)}
                      </ul>
                      <button
                        type="button"
                        className="entity-source"
                        aria-haspopup="dialog"
                        aria-expanded={selectedSourceRef?.id === message.entity.sourceRef.id}
                        onClick={() => {
                          if (message.entity) onOpenSource(message.entity.sourceRef);
                        }}
                      >
                        Источник: {message.entity.sourceRef.fileName} · {formatSourceLocation(message.entity.sourceRef)}
                      </button>
                    </section>
                  )}

                  {message.plan && (
                    <section className="plan-card" aria-label="План правок">
                      <div className="plan-card-heading">
                        <div>
                          <span>План правок</span>
                          <h3>{message.plan.title}</h3>
                        </div>
                        <span className={`plan-status plan-status--${message.plan.status}`}>
                          {message.plan.status === "pending" ? "Ожидает решения" : message.plan.status === "applied" ? "Применён" : "Отменён"}
                        </span>
                      </div>
                      {message.plan.kind === "demo" ? (
                        <ol>{message.plan.changes.map((change) => <li key={change}>{change}</li>)}</ol>
                      ) : (
                        <>
                          <ol>{message.plan.changes.map((change) => <li key={`${change.target_id}:${change.field_path}`}>
                            <strong>{change.target_kind === "entity" ? "Сущность" : "Связь"} · {change.field_path.replaceAll("_", " ")}</strong>
                            <span>{formatChatValue(change.expected_old_value)} → {formatChatValue(change.new_value)}</span>
                            <small>{change.reason}</small>
                          </li>)}</ol>
                          {message.plan.unresolvedQuestions.length > 0 && (
                            <p className="plan-lock-note">Нужно уточнить: {message.plan.unresolvedQuestions.join(" · ")}</p>
                          )}
                        </>
                      )}
                      {message.plan.status === "pending" ? (
                        <>
                          {message.plan.kind === "demo" && !isConfirmed && (
                            <p className="plan-lock-note">Сначала подтвердите версию в разделе «Программа».</p>
                          )}
                          {message.plan.kind === "proposal" && message.plan.expectedVersionId !== currentVersionId && (
                            <p className="plan-lock-note">Предложение относится к предыдущей версии. Отправьте запрос заново.</p>
                          )}
                          <div className="plan-actions">
                            <button
                              type="button"
                              className="plan-apply-button"
                              disabled={message.plan.kind === "demo"
                                ? !isConfirmed
                                : !canUseChat || !hasSignature || isBusy || message.plan.expectedVersionId !== currentVersionId
                                  || message.plan.unresolvedQuestions.length > 0}
                              onClick={() => { void onApplyPlan(message.id); }}
                            >
                              Применить план
                            </button>
                            {message.plan.kind === "demo" && <button
                              type="button"
                              className="plan-cancel-button"
                              onClick={() => onCancelPlan(message.id)}
                            >
                              Отмена
                            </button>}
                          </div>
                        </>
                      ) : message.plan.status === "cancelled" ? (
                        <p className="plan-result-note">План отменён, изменения не внесены.</p>
                      ) : (
                        <div className="validation-card">
                          <div className="validation-card-heading">
                            <strong>Проверка после правок</strong>
                            <span>Завершена</span>
                          </div>
                          <ul>
                            {(message.plan.kind === "demo" ? message.plan.validation ?? [] : [
                              "Изменения сохранены в новой версии. Проверки по графу запущены.",
                            ]).map((result) => (
                              <li className={result.startsWith("⚠") ? "validation-item--warning" : "validation-item--success"} key={result}>
                                {result}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </section>
                  )}
                </div>
              </article>
            ))
          )}
        </div>

      </section>

      <div className="chat-quick-actions" aria-label="Быстрые действия">
        <button type="button" disabled={!canUseChat || isBusy} onClick={() => { void onSend("Проверь часы"); }}>Проверить часы</button>
        <button type="button" disabled={!canUseChat || isBusy} onClick={() => { void onSend("Покажи проблемы"); }}>Показать проблемы</button>
        <button type="button" disabled={!canUseChat || isBusy} onClick={() => { void onSend("Найди дисциплину"); }}>Найти дисциплину</button>
      </div>

      <form
          className="chat-composer"
          onSubmit={(event) => {
            event.preventDefault();
            onSend();
          }}
        >
          <label className="visually-hidden" htmlFor="chat-message">Сообщение агенту</label>
          <textarea
            id="chat-message"
            rows={1}
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            placeholder={isConfirmed ? "Опишите, что нужно изменить, или задайте вопрос…" : "Задайте вопрос о программе…"}
            disabled={!canUseChat || isBusy}
          />
          <button className="chat-send-button" disabled={!canUseChat || isBusy || !draft.trim()} type="submit">
            {isBusy ? "Ожидайте…" : "Отправить"}
          </button>
      </form>

      {selectedSourceRef && (
        <SourcePanel
          sourceRef={selectedSourceRef}
          fragment={demoSourceFragments[selectedSourceRef.id] ?? null}
          onClose={onCloseSource}
        />
      )}
    </>
  );
}

type SourcePanelProps = {
  sourceRef: SourceRef;
  fragment: SourceFragment | null;
  onClose: () => void;
};

function SourcePanel({ sourceRef, fragment, onClose }: SourcePanelProps) {
  const panelRef = useRef<HTMLElement | null>(null);

  return (
    <div className="source-panel-layer">
      <button
        type="button"
        className="source-panel-backdrop"
        aria-label="Закрыть панель источника"
        onClick={onClose}
      />
      <aside
        ref={panelRef}
        className="source-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="source-panel-title"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            onClose();
            return;
          }

          if (event.key === "Tab") {
            const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
              'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
            );
            if (!focusable?.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first.focus();
            }
          }
        }}
      >
        <header className="source-panel-header">
          <div>
            <span className="source-panel-kicker">Фрагмент исходного документа</span>
            <h2 id="source-panel-title">Источник</h2>
          </div>
          <button className="source-panel-close" type="button" autoFocus onClick={onClose} aria-label="Закрыть панель">
            ×
          </button>
        </header>

        <div className="source-panel-content">
          <div className="source-document-meta">
            <strong>{sourceRef.fileName}</strong>
            <span>Зафиксированная версия {sourceRef.versionLabel}</span>
            <span>{formatSourceLocation(sourceRef)}</span>
          </div>

          <p className="source-panel-demo-note">
            Демонстрационный фрагмент. Серверное чтение документа пока не подключено.
          </p>

          {!fragment ? (
            <div className="source-panel-empty" role="status">
              Фрагмент для этого указателя недоступен. Текст не выводится по одному названию раздела.
            </div>
          ) : (
            <>
              {fragment.context.filter((item) => item.relation === "before").map((item, index) => (
                <p className="source-context-paragraph" key={`before-${index}`}>{item.text}</p>
              ))}
              <blockquote className="source-fragment-excerpt">
                <span>Абзац {sourceRef.coordinate.kind === "paragraph" ? sourceRef.coordinate.paragraphIndex : "фрагмент"}</span>
                {fragment.excerpt}
              </blockquote>
              {fragment.context.filter((item) => item.relation === "after").map((item, index) => (
                <p className="source-context-paragraph" key={`after-${index}`}>{item.text}</p>
              ))}
              {fragment.tableRow && (
                <div className="source-table-wrap">
                  <table className="source-fragment-table">
                    <thead><tr>{fragment.tableRow.headers.map((header) => <th key={header}>{header}</th>)}</tr></thead>
                    <tbody><tr>{fragment.tableRow.cells.map((cell, index) => <td key={`${index}-${cell}`}>{cell}</td>)}</tr></tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function App() {
  const previewMode = new URLSearchParams(window.location.search).get("preview");
  const isChatPreview = previewMode === "chat";
  const isReconciliationPreview = previewMode === "reconciliation";
  const isGenerationPreview = previewMode === "generation";
  const hasDemoPreview = isChatPreview || isReconciliationPreview || isGenerationPreview;
  const processing = useImportJob();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const autoCheckAttemptRef = useRef<string | null>(null);
  const [documents, setDocuments] = useState<ProgramDocument[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [notice, setNotice] = useState("");
  const [isDemoMode, setIsDemoMode] = useState(hasDemoPreview);
  const [activeScreen, setActiveScreen] = useState<AppScreen>(
    isChatPreview ? "Чат" : isGenerationPreview ? "Генерация" : "Программа",
  );
  const [demoConfirmed, setDemoConfirmed] = useState(isChatPreview || isGenerationPreview);
  const chatPreviewChoices = isChatPreview
    ? Object.fromEntries(demoProgram.findings.map((finding) => [finding.id, finding.choices[0]?.id ?? ""]))
    : {};
  const [selectedChoices, setSelectedChoices] = useState<Record<string, string>>(chatPreviewChoices);
  const [appliedChoices, setAppliedChoices] = useState<Record<string, string>>(chatPreviewChoices);
  const [customValues, setCustomValues] = useState<Record<string, string>>({});
  const [customCodes, setCustomCodes] = useState<Record<string, string>>({});
  const [findingFilter, setFindingFilter] = useState<FindingFilter>("all");
  const [findingStatusFilter, setFindingStatusFilter] = useState<FindingStatusFilter>("all");
  const [acceptedWarningIds, setAcceptedWarningIds] = useState<string[]>([]);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>(hasDemoPreview ? demoChatMessages : []);
  const [chatDraft, setChatDraft] = useState("");
  const [chatAvailability, setChatAvailability] = useState<"loading" | "available" | "unavailable">("loading");
  const [chatSession, setChatSession] = useState<ChatSessionHistoryRead | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [chatStatusMessage, setChatStatusMessage] = useState("Подключаем чат с агентом…");
  const [signatureDraft, setSignatureDraft] = useState("");
  const [signatureEditing, setSignatureEditing] = useState(false);
  const [selectedSourceRef, setSelectedSourceRef] = useState<SourceRef | null>(null);
  const sourceReturnFocusRef = useRef<HTMLElement | null>(null);
  const parsedProgram = isDemoMode
    ? demoProgram
    : processing.reviewDetails
      ? mapReviewToProgram(processing.reviewDetails, processing.job)
      : null;
  const isConfirmed = isDemoMode ? demoConfirmed : Boolean(processing.reviewDetails?.confirmed);
  const versionNumber = processing.reviewDetails?.version.number ?? 1;
  const hasReconciliation = parsedProgram !== null;
  const unresolvedCount = parsedProgram?.findings.filter((finding) => {
    const appliedChoice = isDemoMode ? appliedChoices[finding.id] : finding.selectedChoiceId;
    const selectedChoice = selectedChoices[finding.id];
    return !appliedChoice || (selectedChoice !== undefined && selectedChoice !== appliedChoice);
  }).length ?? 0;
  const visibleFindings = (parsedProgram?.findings ?? []).filter((finding) => {
    if (findingFilter !== "all" && findingRuleGroup(finding.ruleCode) !== findingFilter) return false;
    const savedChoiceId = isDemoMode ? appliedChoices[finding.id] : finding.selectedChoiceId;
    const draftChoiceId = selectedChoices[finding.id];
    const saved = Boolean(savedChoiceId && (!draftChoiceId || draftChoiceId === savedChoiceId));
    return findingStatusFilter === "all" || (findingStatusFilter === "saved" ? saved : !saved);
  });
  const visibleIssues = (parsedProgram?.issues ?? []).filter((issue) =>
    findingFilter === "all" || issueRuleGroup(issue.ruleCode) === findingFilter,
  );
  const visibleIssueCount = groupDiagnostics(visibleIssues).length;
  const hasUploadedFiles = documents.length > 0 || processing.job !== null;
  const uploadNotice = processing.notice || notice;
  const checkWarnings = processing.checkJob?.status === "completed"
    ? processing.checkJob.report?.issues.filter((issue) => issue.severity === "warning") ?? []
    : [];
  const checkBlockingCount = processing.checkJob?.status === "completed"
    ? processing.checkJob.report?.issues.filter((issue) => issue.severity === "blocking" || issue.requires_decision).length ?? 0
    : 0;
  const checkMatchesCurrentVersion = Boolean(
    processing.checkJob?.status === "completed" && processing.checkJob.report &&
    processing.checkJob.input_version_id === processing.versionId,
  );
  const prepareReport = processing.reviewDetails?.prepare_report ?? null;
  const graphReady = processing.reviewDetails?.version.snapshot.review?.graph_ready === true;
  const canUseLiveChat = !isDemoMode && graphReady && chatAvailability === "available" && chatSession !== null;
  const chatStatus = isDemoMode
    ? ""
    : chatAvailability === "loading"
      ? "Проверяем подключение к модели чата…"
      : chatAvailability === "unavailable"
        ? "Настройки модели чата не готовы. Проверьте CHAT_ENABLED и параметры модели."
        : !graphReady
          ? "Чат станет доступен после построения графа программы."
          : chatSession
            ? ""
            : chatStatusMessage;
  const currentCheckReport = checkMatchesCurrentVersion ? processing.checkJob?.report ?? null : null;
  const displayedCoverage = currentCheckReport?.coverage ?? (graphReady ? [] : prepareReport?.coverage ?? []);
  const displayedValidationIssueCount = groupDiagnostics(
    (currentCheckReport?.issues ?? (graphReady ? [] : prepareReport?.issues ?? []))
      .filter((issue) => !issue.requires_decision).map((issue) => reviewDiagnostic(issue, [])),
  ).length;
  const parserDiagnosticCount = processing.job ? groupDiagnostics(importDiagnostics(processing.job)).length : 0;
  const checkWarningDiagnostics = checkWarnings.map((issue) => {
    const ids = new Set(issue.source_document_ids ?? []);
    for (const ref of processing.reviewDetails?.version.snapshot.source_refs ?? []) {
      if (issue.source_ref_ids.includes(ref.id)) ids.add(ref.document_id);
    }
    return reviewDiagnostic(issue, [...ids].map((id) => ({ id,
      name: processing.job?.files.find((file) => file.document_id === id)?.member_name ?? "Документ пакета",
    })));
  });
  const checkIsRunning = Boolean(
    processing.checkJob && ["pending", "processing"].includes(processing.checkJob.status) &&
    processing.checkJob.input_version_id === processing.versionId,
  );
  const optionalCheckIsRunning = Object.values(processing.optionalRuleChecks).some(
    (check) => check && ["pending", "processing"].includes(check.status),
  );
  const skippedRequiredChecks = checkMatchesCurrentVersion
    ? processing.checkJob?.report?.ruleset.required_rules.filter((ruleCode) => {
      const status = processing.checkJob?.report?.coverage.find((item) => item.rule_code === ruleCode)?.status;
      return status !== "passed" && status !== "failed";
    }) ?? []
    : [];

  useEffect(() => {
    setAcceptedWarningIds([]);
  }, [processing.checkJob?.id]);

  useEffect(() => {
    setSignatureDraft(processing.signature ?? "");
    setSignatureEditing(Boolean(!isDemoMode && processing.program && !processing.signature));
  }, [isDemoMode, processing.program?.id, processing.signature]);

  useEffect(() => {
    if (isDemoMode) {
      setChatAvailability("unavailable");
      return;
    }
    if (!processing.programId) {
      setChatAvailability("loading");
      return;
    }
    let active = true;
    setChatAvailability("loading");
    void api<unknown>(`/api/v1/programs/${processing.programId}/chat/config`)
      .then((value) => {
        if (!active) return;
        if (!isRecord(value) || typeof value.available !== "boolean") {
          setChatAvailability("unavailable");
          return;
        }
        setChatAvailability(value.available ? "available" : "unavailable");
      })
      .catch(() => { if (active) setChatAvailability("unavailable"); });
    return () => { active = false; };
  }, [isDemoMode, processing.programId]);

  useEffect(() => {
    if (isDemoMode) return;
    if (!processing.programId || !graphReady || !processing.versionId || chatAvailability !== "available") {
      setChatSession(null);
      if (!graphReady) setChatMessages([]);
      return;
    }
    let active = true;
    setChatSession(null);
    setChatStatusMessage("Подключаем диалог к текущей версии графа…");
    const connect = async () => {
      const sessions = await api<ChatSessionRead[]>(
        `/api/v1/programs/${processing.programId}/chat/sessions?limit=100`,
      );
      let session = sessions.find((candidate) => candidate.version_id === processing.versionId);
      if (!session) {
        session = await api<ChatSessionRead>(
          `/api/v1/programs/${processing.programId}/chat/sessions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ version_id: processing.versionId }),
          },
        );
      }
      const history = await api<ChatSessionHistoryRead>(
        `/api/v1/programs/${processing.programId}/chat/sessions/${session.id}`,
      );
      if (!active) return;
      setChatSession(history);
      setChatMessages(chatHistoryMessages(history, processing.reviewDetails, processing.job));
      setChatStatusMessage("");
    };
    void connect().catch((error: unknown) => {
      if (!active) return;
      setChatStatusMessage(error instanceof Error ? error.message : "Не удалось открыть чат с агентом.");
    });
    return () => { active = false; };
  }, [isDemoMode, processing.programId, processing.versionId, processing.reviewDetails?.version.id,
    processing.job?.id, graphReady, chatAvailability]);

  useEffect(() => {
    if (processing.reviewJob?.status !== "completed") return;
    setFindingFilter("all");
    setFindingStatusFilter("pending");
  }, [processing.reviewJob?.id, processing.reviewJob?.status]);

  useEffect(() => {
    const details = processing.reviewDetails;
    const versionId = details?.version.id;
    const reviewId = details?.version.snapshot.review?.review_id;
    if (
      isDemoMode || !graphReady || !details || !versionId || !reviewId ||
      processing.versionId !== versionId || processing.reviewBusy || details.confirmed ||
      details.check_job_id || processing.checkJob?.input_version_id === versionId
    ) return;

    const attemptKey = `${reviewId}:${versionId}`;
    if (autoCheckAttemptRef.current === attemptKey) return;
    autoCheckAttemptRef.current = attemptKey;
    void processing.runCheck(`auto-check:${attemptKey}`);
  }, [
    graphReady,
    isDemoMode,
    processing.checkJob?.input_version_id,
    processing.reviewBusy,
    processing.reviewDetails,
    processing.runCheck,
    processing.versionId,
  ]);

  async function addFiles(files: FileList | File[]) {
    if (processing.busy) return;
    setNotice("");
    processing.clearNotice();
    const selectedFiles = Array.from(files);
    const supportedFile = selectedFiles.find(isSupportedFile);

    if (selectedFiles.length > 1) {
      setNotice("Загрузите один ZIP-архив с пакетом документов.");
    } else if (supportedFile) {
      if (!(await processing.upload(supportedFile))) return;
      setDocuments([{ name: supportedFile.name, status: "Загружено" }]);
      setIsDemoMode(false);
      setSelectedChoices({});
      setAppliedChoices({});
      setCustomValues({});
      setCustomCodes({});
      setFindingFilter("all");
      setFindingStatusFilter("pending");
      setDemoConfirmed(false);
      setAcceptedWarningIds([]);
      setChatMessages([]);
      setChatDraft("");
      setActiveScreen("Программа");
      setNotice(`Загружен архив: ${supportedFile.name}.`);
    } else if (selectedFiles.length > 0) {
      setNotice("Поддерживается ZIP-архив.");
    }
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) void addFiles(event.target.files);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragging(false);
    void addFiles(event.dataTransfer.files);
  }

  function chooseFindingChoice(findingId: string, choiceId: string) {
    // Выбор только меняет черновик. Отправка на сервер — отдельное действие.
    setSelectedChoices((current) => ({ ...current, [findingId]: choiceId }));
  }

  function saveFindingChoice(findingId: string, choiceId: string) {
    if (!choiceId || choiceId === "__custom__" || choiceId === "__reject__") return;
    if (isDemoMode) {
      // Только локальное поведение демонстрационного экрана.
      setAppliedChoices((current) => ({ ...current, [findingId]: choiceId }));
      return;
    }
    void processing.decide(findingId, choiceId);
  }

  async function sendChatMessage(message?: string) {
    const text = (message ?? chatDraft).trim();
    if (!text) return;

    if (isDemoMode) {
      if (!hasReconciliation) return;
      const id = `message-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const userMessage: ChatMessage = { id: `${id}-user`, role: "user", text };
      const agentMessage: ChatMessage = {
        id: `${id}-agent`,
        role: "agent",
        text: isConfirmed
          ? "Это демонстрационный ответ. В рабочей версии агент проанализирует запрос и подготовит план правок."
          : "Запрос принят в демо-режиме. До подтверждения версии можно задавать вопросы, а планы правок будут доступны после подтверждения.",
      };
      setChatMessages((current) => [...current, userMessage, agentMessage]);
      if (message === undefined) setChatDraft("");
      return;
    }

    if (!canUseLiveChat || !chatSession || chatBusy || !processing.programId) return;
    const optimisticId = `message-${crypto.randomUUID()}`;
    const userMessage: ChatMessage = { id: `${optimisticId}-user`, role: "user", text };
    const pendingMessage: ChatMessage = { id: `${optimisticId}-agent`, role: "agent", text: "Отправляем запрос агенту…" };
    setChatMessages((current) => [...current, userMessage, pendingMessage]);
    setChatBusy(true);
    if (message === undefined) setChatDraft("");
    try {
      const turn = await api<{ id: string; status: "pending" | "completed" | "failed"; run_id: string }>(
        `/api/v1/programs/${processing.programId}/chat/sessions/${chatSession.id}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ text }),
        },
      );
      const output = await waitForChatOutput(processing.programId, turn.run_id);
      const agentMessage = chatOutputMessage(
        turn.id, output, chatSession.version_id, processing.reviewDetails, processing.job,
      );
      setChatMessages((current) => current.map((item) => item.id === pendingMessage.id ? agentMessage : item));
    } catch (error) {
      const failedMessage: ChatMessage = {
        id: pendingMessage.id,
        role: "agent",
        text: error instanceof Error ? error.message : "Не удалось выполнить запрос к агенту.",
      };
      setChatMessages((current) => current.map((item) => item.id === pendingMessage.id ? failedMessage : item));
    } finally {
      setChatBusy(false);
    }
  }

  async function applyChatPlan(messageId: string) {
    const message = chatMessages.find((item) => item.id === messageId);
    const plan = message?.plan;
    if (!plan || plan.status !== "pending") return;
    if (plan.kind === "demo") {
      if (!isConfirmed) return;
      setChatMessages((current) => current.map((item) => (
        item.id === messageId && item.plan?.kind === "demo" && item.plan.status === "pending"
          ? {
              ...item,
              plan: {
                ...item.plan,
                status: "applied",
                validation: [
                  "✓ Сумма баллов по дисциплине равна 100 (правило R-11).",
                  "⚠ Проверьте согласованность формулировки в связанных документах.",
                ],
              },
            }
          : item
      )));
      return;
    }
    if (!chatSession) return;
    const accepted = await processing.acceptChatProposal(
      chatSession.id, plan.turnId, plan.expectedVersionId,
    );
    if (accepted) {
      setChatMessages((current) => current.map((item) => (
        item.id === messageId && item.plan?.kind === "proposal"
          ? { ...item, plan: { ...item.plan, status: "applied" } }
          : item
      )));
    }
  }

  function cancelChatPlan(messageId: string) {
    setChatMessages((current) => current.map((message) => (
      message.id === messageId && message.plan?.status === "pending"
        ? { ...message, plan: { ...message.plan, status: "cancelled" } }
        : message
    )));
  }

  function openChatSource(sourceRef: SourceRef) {
    const activeElement = document.activeElement;
    sourceReturnFocusRef.current = activeElement instanceof HTMLElement ? activeElement : null;
    setSelectedSourceRef(sourceRef);
  }

  function closeChatSource() {
    setSelectedSourceRef(null);
    requestAnimationFrame(() => sourceReturnFocusRef.current?.focus());
  }

  const pageStatus = isDemoMode && parsedProgram
    ? `Версия ${versionNumber} · подтверждена`
    : isConfirmed
      ? `Версия ${versionNumber} · подтверждена`
      : hasReconciliation
        ? "Готова к сверке"
        : processing.reviewJob && ["pending", "processing"].includes(processing.reviewJob.status)
          ? "Сверка документов"
        : hasUploadedFiles
          ? "Пакет загружен"
          : "Ожидает загрузки";

  return (
    <div className="app-shell">
      <div className="workspace">
        <main className="main-content">
          <nav className="section-navigation" aria-label="Разделы приложения">
            {navigation.map((item) => (
              <button
                type="button"
                key={item}
                className={`nav-item${item === activeScreen ? " is-active" : ""}`}
                aria-current={item === activeScreen ? "page" : undefined}
                onClick={() => setActiveScreen(item)}
              >
                {item}
              </button>
            ))}
          </nav>
          <div className="content-column">
            {!isDemoMode && processing.program && (
              <div className="program-signature" role="group" aria-label="Подпись программы">
                <span>Подпись: <strong>{processing.signature ?? "не указана"}</strong></span>
                {signatureEditing ? (
                  <form className="program-signature-editor" onSubmit={(event) => {
                    event.preventDefault();
                    if (processing.saveSignature(signatureDraft)) setSignatureEditing(false);
                  }}>
                    <label htmlFor="program-signature-input">Имя для подписи</label>
                    <input id="program-signature-input" type="text" maxLength={120} autoFocus
                      value={signatureDraft} onChange={(event) => setSignatureDraft(event.target.value)}
                      disabled={processing.reviewBusy} />
                    <button className="secondary-button" type="submit" disabled={processing.reviewBusy}>Сохранить</button>
                    {processing.signature && <button className="competency-cancel-button" type="button"
                      disabled={processing.reviewBusy} onClick={() => setSignatureEditing(false)}>Отмена</button>}
                  </form>
                ) : (
                  <button className="secondary-button" type="button" onClick={() => {
                    setSignatureDraft(processing.signature ?? "");
                    setSignatureEditing(true);
                  }} disabled={processing.reviewBusy}>
                    Сменить подпись
                  </button>
                )}
              </div>
            )}
            {activeScreen === "Чат" ? (
              <ChatScreen
                versionNumber={versionNumber}
                currentVersionId={processing.versionId}
                isDemoMode={isDemoMode}
                isConfirmed={isConfirmed}
                hasSignature={Boolean(processing.signature)}
                canUseChat={isDemoMode ? hasReconciliation : canUseLiveChat}
                isBusy={chatBusy || processing.reviewBusy}
                statusMessage={chatStatus}
                messages={chatMessages}
                draft={chatDraft}
                selectedSourceRef={selectedSourceRef}
                onDraftChange={setChatDraft}
                onSend={sendChatMessage}
                onApplyPlan={applyChatPlan}
                onCancelPlan={cancelChatPlan}
                onOpenSource={openChatSource}
                onCloseSource={closeChatSource}
              />
            ) : activeScreen === "Генерация" ? (
              <GenerationScreen
                isConfirmed={isConfirmed}
                isDemoMode={isDemoMode}
                versionNumber={versionNumber}
              />
            ) : (
              <>
            <section className="page-heading">
              <div>
                <h1>Программа</h1>
                <p className="eyebrow">Загрузка документов, сверка и подтверждение версии</p>
              </div>
              <span className={`status-pill ${!isDemoMode && isConfirmed ? "status-pill--green" : "status-pill--muted"}`}>
                {pageStatus}
              </span>
            </section>

            <section className="documents-section" aria-labelledby="documents-title">
              <div className="section-heading">
                <h2 id="documents-title">Документы программы</h2>
                <span className="file-count">
                  {hasUploadedFiles
                    ? processing.job?.files.length
                      ? formatFileCount(processing.job.files.length)
                      : "1 ZIP-архив"
                    : isDemoMode && parsedProgram
                      ? formatFileCount(parsedProgram.documentCount)
                      : "Архив не загружен"}
                </span>
              </div>

              <div
                className={`upload-card card${isDragging ? " is-dragging" : ""}`}
                onDragEnter={(event) => {
                  event.preventDefault();
                  setIsDragging(true);
                }}
                onDragOver={(event) => event.preventDefault()}
                onDragLeave={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
                    setIsDragging(false);
                  }
                }}
                onDrop={handleDrop}
              >
                <div className="upload-copy">
                  <strong>Перетащите сюда ZIP-архив с документами</strong>
                  <span>Загрузите весь пакет одним архивом. Название и результаты появятся после распаковки и парсинга.</span>
                </div>
                <input
                  ref={fileInputRef}
                  className="visually-hidden"
                  type="file"
                  accept=".zip,application/zip,application/x-zip-compressed"
                  onChange={handleFileChange}
                  disabled={processing.busy}
                  aria-label="Выбрать ZIP-архив с документами программы"
                />
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={processing.busy}
                >
                  Выбрать ZIP-архив
                </button>
                {uploadNotice && <p className="upload-notice" role="status">{uploadNotice}</p>}
              </div>
            </section>

            {(hasUploadedFiles || hasReconciliation) && (
              <section className="processing-card card" aria-labelledby="processing-title">
                <div className="processing-heading">
                  <div>
                    <h2 id="processing-title">Обработка и сверка</h2>
                    <p className="processing-subtitle">
                      {parsedProgram
                        ? `Все ${formatFileCount(parsedProgram.documentCount)} обработаны. Проверьте расхождения перед подтверждением.`
                        : "Проверка начнётся после загрузки, распаковки и парсинга архива."}
                    </p>
                  </div>
                  {parsedProgram ? (
                    <span className={`status-pill ${unresolvedCount ? "status-pill--warning" : "status-pill--green"}`}>
                      {unresolvedCount > 0 ? `Решений ожидают: ${unresolvedCount} · ` : ""}{formatIssueSummary(parsedProgram.issues)}
                    </span>
                  ) : (
                    <span className="status-pill status-pill--muted">Ожидает обработки</span>
                  )}
                </div>
                {processing.reviewDetails && (
                  <button className="secondary-button" type="button" onClick={processing.retryPrepare} disabled={processing.reviewBusy}>
                    Перезапустить сверку для этого пакета
                  </button>
                )}

                {parsedProgram ? (
                  <>
                    <div className="competency-filter-toolbar" aria-label="Фильтры результатов проверки">
                      <div className="competency-filter-rules" role="group" aria-label="Фильтр по правилу">
                        {(["all", "R-01", "R-07", "R-08", "R-11"] as const).map((filter) => (
                          <button key={filter} type="button"
                            className={`competency-filter-button${findingFilter === filter ? " is-active" : ""}`}
                            aria-pressed={findingFilter === filter}
                            onClick={() => setFindingFilter(filter)}>
                            {filter === "all" ? "Все правила" : filter}
                          </button>
                        ))}
                      </div>
                      <label className="competency-status-filter">
                        Статус
                        <select value={findingStatusFilter} onChange={(event) => setFindingStatusFilter(event.target.value as FindingStatusFilter)}>
                          <option value="all">Все</option>
                          <option value="pending">Требуют решения</option>
                          <option value="saved">Сохранены</option>
                        </select>
                      </label>
                    </div>
                    {processing.reviewDetails && (
                      <section className={`validation-stage-card${checkMatchesCurrentVersion || graphReady ? " validation-stage-card--complete" : ""}`} aria-label="Статус проверки">
                        <div className="validation-stage-heading">
                          <div>
                            <h3>
                              {checkIsRunning
                                ? "Проверки по графу выполняются"
                                : checkMatchesCurrentVersion
                                ? "Проверки по графу завершены"
                                : graphReady
                                  ? "Граф сущностей и связей построен"
                                  : "Предварительная сверка после парсинга"}
                            </h3>
                            <p>
                              {checkMatchesCurrentVersion
                                ? `В полной проверке групп замечаний: ${displayedValidationIssueCount}.`
                                : graphReady
                                  ? "Решения сохранены, граф сущностей и связей собран."
                                  : `Групп замечаний проверки: ${displayedValidationIssueCount}${parserDiagnosticCount ? `; групп сообщений парсера: ${parserDiagnosticCount}` : ""}.`}
                            </p>
                          </div>
                          <span className={`status-pill ${checkMatchesCurrentVersion || graphReady ? "status-pill--green" : "status-pill--warning"}`}>
                            {checkIsRunning ? "Проверка выполняется" : checkMatchesCurrentVersion ? "Проверки завершены" : graphReady ? "Граф готов" : "Предварительный этап"}
                          </span>
                        </div>
                        <div className="validation-stage-metrics">
                          <div><strong>{parsedProgram.documentCount}</strong><span>файлов обработано</span></div>
                          <div><strong>{displayedValidationIssueCount}</strong><span>групп замечаний проверки</span></div>
                          <div><strong>{unresolvedCount}</strong><span>решений ожидают</span></div>
                          <div><strong>{displayedCoverage.length}</strong><span>правил в отчёте</span></div>
                        </div>
                        <p className="validation-stage-message" role="status">
                          {checkIsRunning
                            ? `Проверки по графу выполняются · ${processing.checkJob?.progress ?? 0}%.`
                            : checkMatchesCurrentVersion
                              ? "Результаты относятся к выбранной версии графа."
                              : processing.reviewBusy && unresolvedCount > 0
                                ? "Сохраняем выбор. После последнего обязательного решения система построит граф сущностей и связей."
                                : unresolvedCount > 0
                                  ? `Сохраните решения для ${unresolvedCount} расхождений. После этого система построит граф сущностей и связей.`
                                  : !graphReady
                                    ? "Граф ещё не готов. Завершите обязательные решения и проверьте ошибки построения графа."
                                    : "Граф построен. Автоматические проверки запущены; R-09 и R-11 можно запускать отдельно по запросу."}
                        </p>
                        {!isDemoMode && !checkMatchesCurrentVersion && (
                          <button
                            className="confirm-button validation-run-check-button"
                            type="button"
                            disabled={
                              !graphReady || processing.reviewBusy || checkIsRunning || isConfirmed
                            }
                            onClick={() => void processing.runCheck()}
                          >
                            {checkIsRunning
                              ? "Проверки выполняются…"
                              : processing.reviewBusy
                                ? "Обрабатываем…"
                                : "Запустить автоматические проверки"}
                          </button>
                        )}
                        {displayedCoverage.length > 0 && (
                          <ul className="validation-coverage-list" aria-label="Результаты проверки правил">
                            {displayedCoverage.map((item) => (
                              <li key={item.rule_code}>
                                <strong>{ruleDisplayLabel(item.rule_code)}</strong>
                                <span className={`validation-coverage-status validation-coverage-status--${item.status}`}>
                                  {coverageStatusLabel(item.status)}
                                </span>
                                {item.reason && <small>{item.reason}</small>}
                              </li>
                            ))}
                          </ul>
                        )}
                      </section>
                    )}
                    {!isDemoMode && graphReady && (
                      <section className="optional-rule-checks" aria-labelledby="optional-rule-checks-title">
                        <div className="optional-rule-checks-heading">
                          <div>
                            <h3 id="optional-rule-checks-title">Семантические проверки по запросу</h3>
                            <p>Запускаются только по кнопке после построения графа. Их замечания не блокируют подтверждение версии.</p>
                          </div>
                        </div>
                        <div className="optional-rule-check-grid">
                          {optionalRuleOptions.map(({ code, title, description }) => {
                            const check = processing.optionalRuleChecks[code];
                            const running = Boolean(check && ["pending", "processing"].includes(check.status));
                            const coverage = check?.report?.coverage.find((item) => item.rule_code === code);
                            const issues = check?.report?.issues.filter((issue) => issue.rule_code === code) ?? [];
                            const diagnostics = issues.map((issue: ReviewIssue) => {
                              const documentIds = new Set(issue.source_document_ids ?? []);
                              for (const ref of processing.reviewDetails?.version.snapshot.source_refs ?? []) {
                                if (issue.source_ref_ids.includes(ref.id)) documentIds.add(ref.document_id);
                              }
                              return reviewDiagnostic(issue, [...documentIds].map((id) => ({
                                id,
                                name: processing.job?.files.find((file) => file.document_id === id)?.member_name ?? "Документ пакета",
                              })));
                            });
                            return (
                              <article className="optional-rule-check-card" key={code}>
                                <div>
                                  <span className="optional-rule-code">{code}</span>
                                  <h4>{title}</h4>
                                  <p>{description}</p>
                                </div>
                                <button
                                  className="secondary-button"
                                  type="button"
                                  disabled={isConfirmed || processing.reviewBusy || checkIsRunning || optionalCheckIsRunning}
                                  onClick={() => void processing.runOptionalRuleCheck(code)}
                                >
                                  {running ? "Проверка выполняется…" : check ? "Запустить снова" : "Запустить проверку"}
                                </button>
                                {check?.status === "failed" || check?.status === "conflict" ? (
                                  <p className="optional-rule-check-status optional-rule-check-status--error" role="alert">
                                    {check.message || "Не удалось завершить проверку."}
                                  </p>
                                ) : coverage ? (
                                  <p className={`optional-rule-check-status validation-coverage-status--${coverage.status}`} role="status">
                                    {coverageStatusLabel(coverage.status)}{coverage.reason ? ` · ${coverage.reason}` : ""}
                                  </p>
                                ) : check?.status === "pending" || check?.status === "processing" ? (
                                  <p className="optional-rule-check-status" role="status">Задание поставлено в очередь.</p>
                                ) : null}
                                {diagnostics.length > 0 && <DiagnosticList items={diagnostics} />}
                                {coverage?.status === "passed" && diagnostics.length === 0 && (
                                  <p className="optional-rule-check-empty" role="status">Проблем не найдено.</p>
                                )}
                              </article>
                            );
                          })}
                        </div>
                      </section>
                    )}
                    {findingFilter === "R-08" && <p className="competency-toolbar-note">R-08 группирует формулировки индикаторов с одинаковым кодом в РПД, РПП и ЦКМВ. Отличия в регистре, пробелах, е/ё, краевой пунктуации и принятом сокращении «в т. ч.» не требуют выбора; остальные отличия показываются как варианты для решения пользователя.</p>}
                    {findingFilter === "R-11" && <p className="competency-toolbar-note">R-11 сопоставляет каждый результат обучения в РПД с содержанием раздела 2 и оцениванием из раздела 3. Это смысловая подсказка, а не нормативный вывод: замечания нужно оценить специалисту.</p>}
                    <details className="issues-details" key={`issues-${findingFilter}`} open={findingFilter === "R-08" || findingFilter === "R-11" ? true : undefined}>
                      <summary>Диагностика · групп: {visibleIssueCount}{findingFilter !== "all" ? ` · ${findingFilter}` : ""}</summary>
                      <DiagnosticList items={visibleIssues} />
                    </details>
                    <div className="finding-list finding-accordion">
                      {visibleFindings.length === 0 && <p className="competency-empty-state">По выбранным фильтрам нет расхождений, требующих выбора. {findingFilter === "R-08" ? "Проверьте замечания R-08 ниже." : findingFilter === "R-11" ? "R-11 не предлагает готовый вариант: его замечания показаны ниже для экспертной оценки." : "Попробуйте другой фильтр."}</p>}
                      {visibleFindings.map((finding, index) => {
                        const appliedChoiceId = isDemoMode ? appliedChoices[finding.id] : finding.selectedChoiceId;
                        const appliedChoice = finding.choices.find((choice) => choice.id === appliedChoiceId);
                        const selectedChoiceId = selectedChoices[finding.id] ?? appliedChoiceId ?? "";
                        const isResolved = Boolean(appliedChoice && selectedChoiceId === appliedChoice.id);
                        const ruleGroup = findingRuleGroup(finding.ruleCode);
                        const isChoiceFinding = ruleGroup === "R-01" || ruleGroup === "R-07" || ruleGroup === "R-08";
                        const allowsCustomChoice = ruleGroup === "R-01" || ruleGroup === "R-07";
                        const isCustom = selectedChoiceId === "__custom__";
                        const isReject = selectedChoiceId === "__reject__";
                        const isUnsaved = Boolean(selectedChoiceId && !isResolved);
                        const customText = customValues[finding.id] ?? "";

                        return (
                          <details className={`variant-accordion-item${isResolved ? " is-resolved" : ""}`} key={finding.id} name="validator-variant-findings">
                            <summary className="variant-accordion-summary">
                              <span className="variant-accordion-code">{ruleGroup === "other" ? finding.ruleCode : ruleGroup}</span>
                              <span className="variant-accordion-copy">
                                <strong>{finding.title}</strong>
                                <span>{isChoiceFinding && finding.choices.length > 0
                                  ? `${finding.choices.length} ${formatCountWithWords(finding.choices.length, "вариант", "варианта", "вариантов")}`
                                  : `Расхождение ${index + 1} из ${visibleFindings.length}`}</span>
                              </span>
                              <span className={`finding-status${isResolved ? " finding-status--resolved" : ""}`}>
                                {isResolved ? "Решено" : "Нужно решение"}
                              </span>
                            </summary>
                            <div className="variant-accordion-body">
                            {!isChoiceFinding && <p className="finding-description">{finding.description}</p>}
                            {isChoiceFinding && finding.choices.length > 0 && (
                              <p className="competency-choice-prompt">
                                {ruleGroup === "R-01"
                                  ? "Выберите, какой код оставить:"
                                  : ruleGroup === "R-08"
                                    ? "Выберите формулировку индикатора:"
                                    : "Выберите формулировку компетенции:"}
                              </p>
                            )}
                            {finding.choices.length === 0 && (
                              <p className="finding-help">Для этого расхождения сервер пока не подготовил варианты решения.</p>
                            )}
                            {(finding.choices.length > 0 || allowsCustomChoice) && (
                              <fieldset className="choice-list">
                                <legend className="visually-hidden">Выберите верное значение</legend>
                                {finding.choices.map((choice) => (
                                  <div className="choice-option" key={choice.id}>
                                    <label className={`choice-card${selectedChoiceId === choice.id ? " is-selected" : ""}`}>
                                      <input
                                        type="radio"
                                        name={`choice-${finding.id}`}
                                        value={choice.id}
                                        checked={selectedChoiceId === choice.id}
                                        aria-label={choice.value}
                                        disabled={isConfirmed || processing.reviewBusy}
                                        onChange={() => chooseFindingChoice(finding.id, choice.id)}
                                      />
                                      <span className="choice-content">
                                        <span className="choice-value">
                                          {(ruleGroup === "R-07" || ruleGroup === "R-08") && finding.choices.length > 1
                                            ? <ComparisonText value={choice.value} otherValues={finding.choices.filter((item) => item.id !== choice.id).map((item) => item.value)} />
                                            : choice.value}
                                        </span>
                                        {isChoiceFinding && choice.documentCount > 0 && (
                                          <span className="choice-document-count">{formatCountWithWords(choice.documentCount, "документ", "документа", "документов")}</span>
                                        )}
                                      </span>
                                    </label>
                                    {isChoiceFinding && choice.documents.length > 0 && (
                                      <details className="choice-sources">
                                        <summary>Показать документы</summary>
                                        <ul>{choice.documents.map((document) => <li key={document}>{document}</li>)}</ul>
                                      </details>
                                    )}
                                  </div>
                                ))}
                                {(ruleGroup === "R-07" || ruleGroup === "R-08") && finding.choices.length > 1 && (
                                  <p className="competency-comparison-hint">Подсветка показывает текстовые отличия между формулировками.</p>
                                )}
                                {allowsCustomChoice && (
                                  <>
                                    <label className={`choice-card${isCustom ? " is-selected" : ""}`}>
                                      <input
                                        type="radio"
                                        name={`choice-${finding.id}`}
                                        value="__custom__"
                                        checked={isCustom}
                                        disabled={isConfirmed || processing.reviewBusy}
                                        onChange={() => chooseFindingChoice(finding.id, "__custom__")}
                                      />
                                      <span className="choice-content"><span className="choice-value">Ввести свой вариант</span></span>
                                    </label>
                                    {finding.ruleCode === "R-01" && (
                                      <label className={`choice-card${isReject ? " is-selected" : ""}`}>
                                        <input
                                          type="radio"
                                          name={`choice-${finding.id}`}
                                          value="__reject__"
                                          checked={isReject}
                                          disabled={isConfirmed || processing.reviewBusy}
                                          onChange={() => chooseFindingChoice(finding.id, "__reject__")}
                                        />
                                        <span className="choice-content"><span className="choice-value">Отклонить сопоставление</span></span>
                                      </label>
                                    )}
                                  </>
                                )}
                              </fieldset>
                            )}
                            {allowsCustomChoice && isCustom && (
                              <div className="competency-custom-editor">
                                {finding.ruleCode === "R-07" && (
                                  <>
                                    <label htmlFor={`custom-code-${finding.id}`}>Итоговый код компетенции</label>
                                    <input
                                      id={`custom-code-${finding.id}`}
                                      type="text"
                                      maxLength={200}
                                      value={customCodes[finding.id] ?? ""}
                                      disabled={isConfirmed || processing.reviewBusy}
                                      placeholder="Введите код, если его нужно изменить"
                                      onChange={(event) => setCustomCodes((current) => ({ ...current, [finding.id]: event.target.value }))}
                                    />
                                  </>
                                )}
                                <label htmlFor={`custom-${finding.id}`}>
                                  {finding.ruleCode === "R-01" ? "Собственный код компетенции" : "Собственная формулировка компетенции"}
                                </label>
                                <textarea
                                  id={`custom-${finding.id}`}
                                  rows={finding.ruleCode === "R-01" ? 2 : 4}
                                  value={customText}
                                  maxLength={4000}
                                  disabled={isConfirmed || processing.reviewBusy}
                                  placeholder={finding.ruleCode === "R-01" ? "Введите код" : "Введите формулировку"}
                                  onChange={(event) => setCustomValues((current) => ({ ...current, [finding.id]: event.target.value }))}
                                />
                              </div>
                            )}
                            {isChoiceFinding && selectedChoiceId && !isResolved && (
                              <section className="competency-decision-preview" aria-label="Предпросмотр выбранного решения" aria-live="polite">
                                <strong>Предпросмотр решения · не сохранено</strong>
                                {isReject ? <p>Отклонить предложенное сопоставление кодов.</p>
                                  : isCustom ? <>
                                    {ruleGroup === "R-07" && <p><span>Код:</span> {customCodes[finding.id]?.trim() || "Без изменения кода"}</p>}
                                    <p><span>{ruleGroup === "R-01" ? "Код:" : "Формулировка:"}</span> {customText.trim() || "Значение не введено"}</p>
                                  </> : <>
                                    <p>{finding.choices.find((choice) => choice.id === selectedChoiceId)?.value ?? "Вариант не найден"}</p>
                                    <small>{finding.choices.find((choice) => choice.id === selectedChoiceId)?.documents.join(" · ")}</small>
                                  </>}
                                <p className="competency-preview-notice">Выбор не изменяет исходные документы. Сохранение выполняется только после отдельного подтверждения и ответа сервера.</p>
                              </section>
                            )}
                            {(finding.choices.length > 0 || allowsCustomChoice) && !isConfirmed && (
                              <div className="competency-action-row">
                                <button
                                  className="secondary-button"
                                  type="button"
                                  disabled={!isUnsaved || isCustom || isReject || processing.reviewBusy}
                                  onClick={() => saveFindingChoice(finding.id, selectedChoiceId)}
                                >
                                  {processing.reviewBusy ? "Сохраняем…" : "Подтвердить и сохранить выбор"}
                                </button>
                                {isUnsaved && (
                                  <button className="competency-cancel-button" type="button"
                                    disabled={processing.reviewBusy}
                                    onClick={() => setSelectedChoices((current) => {
                                      const next = { ...current };
                                      if (appliedChoiceId) next[finding.id] = appliedChoiceId;
                                      else delete next[finding.id];
                                      return next;
                                    })}
                                  >Отменить выбор</button>
                                )}
                                {isResolved && <span className="competency-saved-note">Выбор сохранён в версии</span>}
                              </div>
                            )}
                            {(isCustom || isReject) && (
                              <p className="competency-api-note" role="status">
                                {isCustom && !customText.trim() ? "Введите значение. " : ""}
                                Сохранение {isReject ? "отклонения" : "собственного варианта"} пока недоступно:
                                действующий API Review принимает только идентификатор готового варианта.
                                Введённое значение остаётся черновиком и не отправляется на сервер.
                              </p>
                            )}
                            </div>
                          </details>
                        );
                      })}
                    </div>

                    {!isDemoMode && processing.checkJob && ["pending", "processing"].includes(processing.checkJob.status) && (
                      <div className="processing-caption" role="status">
                        Повторная проверка версии выполняется · {processing.checkJob.progress}%
                        <progress max={100} value={processing.checkJob.progress} aria-label="Прогресс повторной проверки" />
                      </div>
                    )}

                    {!isDemoMode && checkMatchesCurrentVersion && checkWarnings.length > 0 && (
                      <section className="warning-acknowledgements" aria-label="Принятие предупреждений">
                        <h3>Подтвердите, что группы предупреждений учтены</h3>
                        <p>Подтверждение группы относится ко всем указанным в ней записям. Подробности доступны в каждой группе.</p>
                        <DiagnosticList items={checkWarningDiagnostics} acceptance={{
                          acceptedIds: acceptedWarningIds, disabled: isConfirmed || processing.reviewBusy,
                          onChange: (ids, accepted) => setAcceptedWarningIds((current) => updateAcceptedWarnings(current, ids, accepted)),
                        }} />
                      </section>
                    )}

                    {!isDemoMode && checkMatchesCurrentVersion && checkBlockingCount > 0 && (
                      <p className="upload-notice" role="alert">
                        Проверка нашла {checkBlockingCount} блокирующих замечаний. Исправьте их перед подтверждением версии.
                      </p>
                    )}
                    {!isDemoMode && skippedRequiredChecks.length > 0 && (
                      <p className="upload-notice" role="alert">
                        Не выполнены обязательные проверки: {skippedRequiredChecks.join(", ")}.
                      </p>
                    )}

                    <div className="confirm-version">
                      <div className="confirm-copy">
                        <strong>{isConfirmed ? "Версия программы подтверждена" : "Готовы зафиксировать версию?"}</strong>
                        <span>
                          {isConfirmed
                            ? isDemoMode ? "Результат подтверждения сохранён в предпросмотре." : "Подтверждённая версия сохранена на сервере."
                            : unresolvedCount
                              ? `Сначала примените решение для ${unresolvedCount} ${unresolvedCount === 1 ? "расхождения" : "расхождений"}.`
                              : !isDemoMode && !checkMatchesCurrentVersion
                                ? "Сначала запустите проверки по построенному графу."
                                : !isDemoMode && checkBlockingCount > 0
                                  ? "Проверка нашла блокирующие замечания. Эту версию нельзя подтвердить."
                                  : !isDemoMode && skippedRequiredChecks.length > 0
                                  ? "Не все обязательные проверки выполнены, версия пока не подтверждена."
                                    : !isDemoMode && checkWarnings.some((warning) => !acceptedWarningIds.includes(warning.id))
                                    ? "Подтвердите, что все группы предупреждений учтены."
                                    : "После подтверждения эта версия станет основой для работы с программой."}
                        </span>
                      </div>
                      <button
                        className="confirm-button"
                        disabled={
                          unresolvedCount > 0 || isConfirmed || processing.reviewBusy ||
                          (!isDemoMode && (!checkMatchesCurrentVersion || checkBlockingCount > 0 ||
                            skippedRequiredChecks.length > 0 ||
                            checkWarnings.some((warning) => !acceptedWarningIds.includes(warning.id))))
                        }
                        onClick={() => {
                          if (isDemoMode) setDemoConfirmed(true);
                          else void processing.confirm(acceptedWarningIds);
                        }}
                        type="button"
                      >
                        {isConfirmed
                          ? "Версия подтверждена"
                          : processing.reviewBusy
                            ? "Обрабатываем…"
                            : !isDemoMode && checkMatchesCurrentVersion && checkBlockingCount > 0
                              ? "Есть блокирующие замечания"
                              : !isDemoMode && checkMatchesCurrentVersion && skippedRequiredChecks.length > 0
                                ? "Проверки не завершены"
                                : isDemoMode || checkMatchesCurrentVersion
                                  ? "Подтвердить версию"
                                  : "Сначала проверьте граф"}
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    {documents.length > 0 && (
                      <ul className="document-list">
                        {documents.map((document) => (
                          <li className="document-row" key={document.name}>
                            <span className="document-name">{document.name}</span>
                            <span className="status-pill status-pill--green document-status">{document.status}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {processing.job && (
                      <ImportProgress
                        programId={processing.programId}
                        job={processing.job}
                        busy={processing.busy}
                        onRetry={processing.retry}
                      />
                    )}
                    {processing.reviewJob && (
                      <div className="processing-caption" role="status">
                        {processing.reviewJob.status === "completed"
                          ? "Сверка завершена. Загружаем результаты…"
                          : processing.reviewJob.status === "failed" || processing.reviewJob.status === "conflict"
                            ? processing.reviewJob.message || "Не удалось подготовить сверку."
                            : `Подготавливаем сверку · ${processing.reviewJob.progress}%`}
                        {!["completed", "failed", "conflict"].includes(processing.reviewJob.status) && (
                          <progress max={100} value={processing.reviewJob.progress} aria-label="Прогресс подготовки сверки" />
                        )}
                      </div>
                    )}
                    {processing.canPrepareReview && processing.notice && (!processing.reviewJob || ["failed", "conflict"].includes(processing.reviewJob.status)) && (
                      <button className="secondary-button" type="button" onClick={processing.retryPrepare} disabled={processing.reviewBusy}>
                        Повторить подготовку сверки
                      </button>
                    )}
                    <p className="processing-caption">
                      {processing.job?.issues.some((issue) => issue.severity === "error")
                        ? "В архиве есть ошибки разбора. Исправьте исходные документы и загрузите пакет заново."
                        : processing.reviewBusy
                          ? "Пакет разобран. Подготавливаем результаты сверки."
                          : "Название программы, извлечённые данные и расхождения появятся после завершения обработки архива."}
                    </p>
                  </>
                )}
              </section>
            )}
              </>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
