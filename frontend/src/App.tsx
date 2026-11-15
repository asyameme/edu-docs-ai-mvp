import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { ImportProgress } from "./ImportProgress";
import { GenerationScreen } from "./GenerationScreen";
import { useImportJob } from "./useImportJob";
import type { ImportJob, ReviewDetails, ReviewIssue } from "./api";

type ProgramDocument = {
  name: string;
  status: string;
};

type FindingChoice = {
  id: string;
  source: string;
  value: string;
  location: string;
};

type ReconciliationFinding = {
  id: string;
  title: string;
  description: string;
  choices: FindingChoice[];
  selectedChoiceId?: string;
};

type ValidationIssue = {
  id: string;
  severity: "error" | "warning" | "info";
  title: string;
  source: string;
  ruleId?: string;
};

type ParsedProgram = {
  documentCount: number;
  findings: ReconciliationFinding[];
  issues: ValidationIssue[];
};

type ChatPlan = {
  id: string;
  title: string;
  changes: string[];
  status: "pending" | "applied" | "cancelled";
  validation?: string[];
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
};

type AppScreen = "Программа" | "Чат" | "Генерация";

const demoProgram: ParsedProgram = {
  documentCount: 12,
  findings: [
    {
      id: "competence-code",
      title: "Код компетенции различается",
      description: "План: УК-2 · РПД: УК-3 | Учебный план, стр. 8 · РПД, разд. 3.2",
      choices: [
        {
          id: "study-plan",
          source: "Учебный план.docx",
          value: "Оставить УК-2",
          location: "стр. 8",
        },
        {
          id: "rpd",
          source: "РПД_Анализ_данных.docx",
          value: "Оставить УК-3",
          location: "разд. 3.2",
        },
        {
          id: "both",
          source: "Учебный план.docx · РПД_Анализ_данных.docx",
          value: "Оба варианта",
          location: "стр. 8 · разд. 3.2",
        },
      ],
    },
  ],
  issues: [
    {
      id: "issue-1",
      severity: "error",
      title: "Для компетенции не найден индикатор достижения",
      source: "РПД_Анализ_данных.docx · раздел 4",
      ruleId: "R-04",
    },
    {
      id: "issue-2",
      severity: "warning",
      title: "Формулировка результата обучения отличается от учебного плана",
      source: "Учебный план.docx · таблица «Результаты обучения»",
      ruleId: "R-11",
    },
    {
      id: "issue-3",
      severity: "warning",
      title: "В документах различаются формулировки результатов обучения",
      source: "Учебный план.docx · таблица «Результаты обучения»",
      ruleId: "R-11",
    },
  ],
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

function formatIssueSummary(issues: ValidationIssue[]) {
  const errors = issues.filter((issue) => issue.severity === "error").length;
  const warnings = issues.filter((issue) => issue.severity === "warning").length;
  return [
    errors > 0 ? formatCountWithWords(errors, "ошибка", "ошибки", "ошибок") : "",
    warnings > 0 ? formatCountWithWords(warnings, "предупреждение", "предупреждения", "предупреждений") : "",
  ].filter(Boolean).join(" · ") || "Проверка завершена";
}

function mapReviewToProgram(details: ReviewDetails, job: ImportJob | null): ParsedProgram {
  const snapshot = details.version.snapshot;
  const sourceRefs = new Map(snapshot.source_refs.map((source) => [source.id, source]));
  const documentNames = new Map((job?.files ?? []).map((file) => [file.document_id, file.member_name]));
  const documentCount = job?.files.length ?? snapshot.source_refs.length;
  const sourceDescription = (ids: string[]) => {
    const sources = ids.map((id) => sourceRefs.get(id)).filter((source) => source !== undefined);
    const names = [...new Set(sources.map((source) => documentNames.get(source.document_id)).filter((name) => name !== undefined))];
    const sections = [...new Set(sources.map((source) => source.section).filter((section) => section !== null))];
    return {
      source: names.length ? names.join(" · ") : "Источник пакета документов",
      location: sections.length ? sections.join(" · ") : "Фрагмент исходного документа",
    };
  };
  const findings = details.issues.filter((issue) => issue.requires_decision)
    .map((issue) => {
      const choices = issue.variants.map((variant) => {
        const changeSourceIds = variant.changes.flatMap((change) => [
          ...(change.entity?.source_ref_ids ?? []),
          ...(change.relation?.source_ref_ids ?? []),
        ]);
        const location = sourceDescription(changeSourceIds.length ? changeSourceIds : issue.source_ref_ids);
        return { id: variant.id, value: variant.label, ...location };
      });
      const choiceSummary = choices.map((choice) => choice.value).join(" · ");
      const sourceSummary = [...new Set(choices.map((choice) => `${choice.source}, ${choice.location}`))].join(" · ");
      return {
        id: issue.id,
        title: issue.message,
        description: choiceSummary
          ? `${choiceSummary}${sourceSummary ? ` | ${sourceSummary}` : ""}`
          : `Правило ${issue.rule_code} · ${issue.type}`,
        selectedChoiceId: issue.decision?.variant_id,
        choices,
      };
    });
  const reviewIssues = details.issues.map((issue: ReviewIssue) => {
    const source = sourceDescription(issue.source_ref_ids);
    return {
      id: issue.id,
      severity: issue.severity === "blocking" ? "error" as const : "warning" as const,
      title: issue.message,
      source: source.source,
      ruleId: `${issue.rule_code} · ${issue.rule_revision}`,
    };
  });
  const importIssues = (job?.issues ?? []).map((issue, index) => ({
    id: `import-${index}-${issue.category}`,
    severity: issue.severity === "error" ? "error" as const : "warning" as const,
    title: issue.message,
    source: "Разбор ZIP-пакета",
    ruleId: issue.category,
  }));

  return {
    documentCount,
    findings,
    issues: [...reviewIssues, ...importIssues],
  };
}

type ChatScreenProps = {
  versionNumber: number;
  isDemoMode: boolean;
  isConfirmed: boolean;
  canUseChat: boolean;
  messages: ChatMessage[];
  draft: string;
  selectedSourceRef: SourceRef | null;
  onDraftChange: (value: string) => void;
  onSend: (message?: string) => void;
  onApplyPlan: (messageId: string) => void;
  onCancelPlan: (messageId: string) => void;
  onOpenSource: (sourceRef: SourceRef) => void;
  onCloseSource: () => void;
};

function ChatScreen({
  versionNumber,
  isDemoMode,
  isConfirmed,
  canUseChat,
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
              <h2>{canUseChat ? "Начните диалог с агентом" : "Чат с агентом пока не подключён"}</h2>
              <p>
                {canUseChat
                  ? "Задайте вопрос по документам или опишите нужное изменение."
                  : "После подключения агента здесь появятся ответы, проверки и планы правок по программе."}
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
                      <ol>
                        {message.plan.changes.map((change) => <li key={change}>{change}</li>)}
                      </ol>
                      {message.plan.status === "pending" ? (
                        <>
                          {!isConfirmed && (
                            <p className="plan-lock-note">Сначала подтвердите версию в разделе «Программа».</p>
                          )}
                          <div className="plan-actions">
                            <button
                              type="button"
                              className="plan-apply-button"
                              disabled={!isConfirmed}
                              onClick={() => onApplyPlan(message.id)}
                            >
                              Применить план
                            </button>
                            <button
                              type="button"
                              className="plan-cancel-button"
                              onClick={() => onCancelPlan(message.id)}
                            >
                              Отмена
                            </button>
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
                            {(message.plan.validation ?? []).map((result) => (
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
        <button type="button" disabled={!canUseChat} onClick={() => onSend("Проверь часы")}>Проверить часы</button>
        <button type="button" disabled={!canUseChat} onClick={() => onSend("Покажи проблемы")}>Показать проблемы</button>
        <button type="button" disabled={!canUseChat} onClick={() => onSend("Найди дисциплину")}>Найти дисциплину</button>
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
            disabled={!canUseChat}
          />
          <button className="chat-send-button" disabled={!canUseChat || !draft.trim()} type="submit">
            Отправить
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
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const closeSidebarButtonRef = useRef<HTMLButtonElement>(null);
  const openSidebarButtonRef = useRef<HTMLButtonElement>(null);
  const previousSidebarOpenRef = useRef(isSidebarOpen);
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
  const [acceptedWarningIds, setAcceptedWarningIds] = useState<string[]>([]);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>(hasDemoPreview ? demoChatMessages : []);
  const [chatDraft, setChatDraft] = useState("");
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
    if (previousSidebarOpenRef.current === isSidebarOpen) return;
    previousSidebarOpenRef.current = isSidebarOpen;
    if (isSidebarOpen) closeSidebarButtonRef.current?.focus();
    else openSidebarButtonRef.current?.focus();
  }, [isSidebarOpen]);

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
    setSelectedChoices((current) => ({ ...current, [findingId]: choiceId }));
    if (isDemoMode) {
      setAppliedChoices((current) => ({ ...current, [findingId]: choiceId }));
      return;
    }
    void processing.decide(findingId, choiceId);
  }

  function sendChatMessage(message?: string) {
    const text = (message ?? chatDraft).trim();
    if (!text || !isDemoMode || !hasReconciliation) return;

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
  }

  function applyChatPlan(messageId: string) {
    if (!isConfirmed) return;
    setChatMessages((current) => current.map((message) => (
      message.id === messageId && message.plan?.status === "pending"
        ? {
            ...message,
            plan: {
              ...message.plan,
              status: "applied",
              validation: [
                "✓ Сумма баллов по дисциплине равна 100 (правило R-11).",
                "⚠ Проверьте согласованность формулировки в связанных документах.",
              ],
            },
          }
        : message
    )));
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
        <aside
          className={"sidebar" + (isSidebarOpen ? "" : " sidebar--closed")}
          aria-label="Разделы приложения"
          aria-hidden={!isSidebarOpen}
          inert={!isSidebarOpen}
        >
          <div className="sidebar-heading">
            <span>Рабочее пространство</span>
            <button
              ref={closeSidebarButtonRef}
              className="sidebar-toggle"
              type="button"
              aria-label="Свернуть боковую панель"
              aria-expanded={isSidebarOpen}
              title="Свернуть боковую панель"
              onClick={() => setIsSidebarOpen(false)}
            >
              ‹
            </button>
          </div>
          <nav className="side-nav">
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
        </aside>

        <main className="main-content">
          {!isSidebarOpen && (
            <div className="collapsed-navigation">
              <button
                ref={openSidebarButtonRef}
                className="sidebar-toggle sidebar-toggle--open"
                type="button"
                aria-label="Открыть боковую панель"
                aria-expanded={isSidebarOpen}
                title="Открыть боковую панель"
                onClick={() => setIsSidebarOpen(true)}
              >
                ☰
              </button>
              <nav className="side-nav side-nav--horizontal" aria-label="Разделы приложения">
                {navigation.map((item) => (
                  <button
                    type="button"
                    key={item}
                    className={"nav-item" + (item === activeScreen ? " is-active" : "")}
                    aria-current={item === activeScreen ? "page" : undefined}
                    onClick={() => setActiveScreen(item)}
                  >
                    {item}
                  </button>
                ))}
              </nav>
            </div>
          )}
          <div className="content-column">
            {!isDemoMode && processing.program && (
              <div className="program-signature" role="group" aria-label="Подпись программы">
                <span>Подпись: <strong>{processing.signature ?? "не указана"}</strong></span>
                <button className="secondary-button" type="button" onClick={processing.changeSignature} disabled={processing.reviewBusy}>
                  {processing.signature ? "Сменить подпись" : "Указать подпись"}
                </button>
              </div>
            )}
            {activeScreen === "Чат" ? (
              <ChatScreen
                versionNumber={versionNumber}
                isDemoMode={isDemoMode}
                isConfirmed={isConfirmed}
                canUseChat={isDemoMode && hasReconciliation}
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
                      {formatIssueSummary(parsedProgram.issues)}
                    </span>
                  ) : (
                    <span className="status-pill status-pill--muted">Ожидает обработки</span>
                  )}
                </div>

                {parsedProgram ? (
                  <>
                    <div className="finding-list">
                      {parsedProgram.findings.map((finding, index) => {
                        const appliedChoiceId = isDemoMode ? appliedChoices[finding.id] : finding.selectedChoiceId;
                        const appliedChoice = finding.choices.find((choice) => choice.id === appliedChoiceId);
                        const selectedChoiceId = selectedChoices[finding.id] ?? appliedChoiceId ?? "";
                        const isResolved = Boolean(appliedChoice && selectedChoiceId === appliedChoice.id);

                        return (
                          <div className="finding-group" key={finding.id}>
                            <article className={`finding-card${isResolved ? " is-resolved" : ""}`}>
                            <div className="finding-topline">
                              <span className="finding-label">Расхождение {index + 1} из {parsedProgram.findings.length}</span>
                              <span className={`finding-status${isResolved ? " finding-status--resolved" : ""}`}>
                                {isResolved ? "Решено" : "Нужно решение"}
                              </span>
                            </div>
                            <h3>{finding.title}</h3>
                            <p className="finding-description">{finding.description}</p>
                            {finding.choices.length === 0 && (
                              <p className="finding-help">Для этого расхождения сервер пока не подготовил варианты решения.</p>
                            )}
                          </article>
                            {finding.choices.length > 0 && (
                              <fieldset className="choice-list">
                                <legend className="visually-hidden">Выберите верное значение</legend>
                                {finding.choices.map((choice) => (
                                  <label
                                    className={`choice-card${selectedChoiceId === choice.id ? " is-selected" : ""}`}
                                    key={choice.id}
                                    title={`${choice.source} · ${choice.location}`}
                                  >
                                    <input
                                      type="radio"
                                      name={`choice-${finding.id}`}
                                      value={choice.id}
                                      checked={selectedChoiceId === choice.id}
                                      aria-label={`${choice.value} · ${choice.source} · ${choice.location}`}
                                      disabled={isConfirmed || processing.reviewBusy}
                                      onChange={() => chooseFindingChoice(finding.id, choice.id)}
                                    />
                                    <span className="choice-content">
                                      <span className="choice-value">{choice.value}</span>
                                      <span className="choice-source">{choice.source}</span>
                                      <span className="choice-location">{choice.location}</span>
                                    </span>
                                  </label>
                                ))}
                              </fieldset>
                            )}
                          </div>
                        );
                      })}
                    </div>

                    <details className="issues-details">
                      <summary>Замечания проверки · {parsedProgram.issues.length}</summary>
                      <ul className="issue-list">
                        {parsedProgram.issues.map((issue) => (
                          <li className="issue-row" key={issue.id}>
                            <span className={`severity-dot severity-dot--${issue.severity}`} aria-hidden="true" />
                            <span className="issue-copy">
                              <strong>{issue.title}</strong>
                              <span>{issue.source}{issue.ruleId ? ` · ${issue.ruleId}` : ""}</span>
                            </span>
                            <span className={`severity-label severity-label--${issue.severity}`}>
                              {issue.severity === "error" ? "Ошибка" : issue.severity === "warning" ? "Предупреждение" : "Информация"}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </details>

                    {!isDemoMode && processing.checkJob && ["pending", "processing"].includes(processing.checkJob.status) && (
                      <div className="processing-caption" role="status">
                        Повторная проверка версии выполняется · {processing.checkJob.progress}%
                        <progress max={100} value={processing.checkJob.progress} aria-label="Прогресс повторной проверки" />
                      </div>
                    )}

                    {!isDemoMode && checkMatchesCurrentVersion && checkWarnings.length > 0 && (
                      <fieldset className="choice-list" aria-label="Принятие предупреждений">
                        <legend>Подтвердите, что предупреждения учтены</legend>
                        {checkWarnings.map((warning) => (
                          <label className="choice-card" key={warning.id}>
                            <input
                              type="checkbox"
                              checked={acceptedWarningIds.includes(warning.id)}
                              disabled={isConfirmed || processing.reviewBusy}
                              onChange={(event) => setAcceptedWarningIds((current) => (
                                event.target.checked
                                  ? [...new Set([...current, warning.id])]
                                  : current.filter((id) => id !== warning.id)
                              ))}
                            />
                            <span className="choice-content">
                              <span className="choice-value">{warning.message}</span>
                              <span className="choice-source">Предупреждение проверки</span>
                            </span>
                          </label>
                        ))}
                      </fieldset>
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
                                ? "Сначала запустите повторную проверку выбранной версии."
                                : !isDemoMode && checkBlockingCount > 0
                                  ? "Проверка нашла блокирующие замечания. Эту версию нельзя подтвердить."
                                : !isDemoMode && skippedRequiredChecks.length > 0
                                  ? "Не все обязательные проверки выполнены, версия пока не подтверждена."
                                  : !isDemoMode && checkWarnings.some((warning) => !acceptedWarningIds.includes(warning.id))
                                    ? "Примите каждое предупреждение, чтобы подтвердить проверенную версию."
                                    : "После подтверждения эта версия станет основой для работы с программой."}
                        </span>
                      </div>
                      <button
                        className="confirm-button"
                        disabled={unresolvedCount > 0 || isConfirmed || processing.reviewBusy || (!isDemoMode && checkMatchesCurrentVersion && (checkBlockingCount > 0 || skippedRequiredChecks.length > 0 || checkWarnings.some((warning) => !acceptedWarningIds.includes(warning.id))))}
                        onClick={() => {
                          if (isDemoMode) setDemoConfirmed(true);
                          else if (checkMatchesCurrentVersion) void processing.confirm(acceptedWarningIds);
                          else void processing.runCheck();
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
                              : "Проверить версию"}
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
