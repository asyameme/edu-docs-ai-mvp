import { useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { ImportProgress } from "./ImportProgress";
import { useImportJob } from "./useImportJob";

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
};

type ValidationIssue = {
  id: string;
  severity: "error" | "warning" | "info";
  title: string;
  source: string;
  ruleId?: string;
};

type ParsedProgram = {
  name: string;
  summary: string;
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
  entity?: {
    name: string;
    details: string[];
    sourceRef: SourceRef;
  };
  plan?: ChatPlan;
};

type AppScreen = "Программа" | "Чат";

const demoProgram: ParsedProgram = {
  name: "Пример образовательной программы",
  summary: "Демонстрационные данные · бакалавриат · версия 1",
  documentCount: 12,
  findings: [
    {
      id: "competence-code",
      title: "Код компетенции различается",
      description: "Выберите значение, которое следует сохранить в версии программы.",
      choices: [
        {
          id: "study-plan",
          source: "Учебный план.docx",
          value: "УК-2",
          location: "Раздел «Компетенции», строка 14",
        },
        {
          id: "rpd",
          source: "РПД_Анализ_данных.docx",
          value: "УК-3",
          location: "Раздел «Результаты обучения», таблица 2",
        },
      ],
    },
    {
      id: "discipline-hours",
      title: "Не совпадает объём дисциплины",
      description: "В документах указано разное количество контактных часов.",
      choices: [
        {
          id: "study-plan-hours",
          source: "Учебный план.docx",
          value: "72 часа",
          location: "Семестр 3, строка «Основы анализа данных»",
        },
        {
          id: "rpd-hours",
          source: "РПД_Анализ_данных.docx",
          value: "68 часов",
          location: "Раздел «Объём дисциплины», таблица 1",
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
      severity: "info",
      title: "Документ успешно разобран",
      source: "Характеристика_программы.docx",
    },
  ],
};

const navigation = ["Программа", "Чат", "Выгрузка"] as const;

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
  const lastTwoDigits = count % 100;
  const lastDigit = count % 10;
  if (lastTwoDigits >= 11 && lastTwoDigits <= 14) return `${count} файлов`;
  if (lastDigit === 1) return `${count} файл`;
  if (lastDigit >= 2 && lastDigit <= 4) return `${count} файла`;
  return `${count} файлов`;
}

type ChatScreenProps = {
  programName: string;
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
  programName,
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
          <p className="eyebrow">Программа «{programName}» · версия v1</p>
        </div>
        <span className={`status-pill ${!isDemoMode && isConfirmed ? "status-pill--green" : "status-pill--muted"}`}>
          {isDemoMode ? "Демо-сценарий" : isConfirmed ? "Версия подтверждена" : "Только чтение"}
        </span>
      </section>

      {isDemoMode && (
        <div className="demo-banner" role="status">
          Демо-чат: сообщения и план изменений примерные. Ответы реального агента пока не подключены.
        </div>
      )}

      <section className="chat-panel card" aria-label="Чат с агентом">
        <div className="chat-panel-heading">
          <div>
            <strong>История диалога</strong>
            <span>{isConfirmed ? "Можно обсуждать и готовить правки" : "Можно задавать вопросы о программе"}</span>
          </div>
          <span className={`agent-presence ${isDemoMode ? "agent-presence--demo" : "agent-presence--offline"}`}>
            <span /> {isDemoMode ? "Пример диалога" : "Ожидает подключения"}
          </span>
        </div>

        <div className="chat-transcript" aria-live="polite" aria-relevant="additions text">
          {messages.length === 0 ? (
            <div className="chat-empty-state">
              <div className="agent-mark" aria-hidden="true">ИИ</div>
              <h2>{canUseChat ? "Начните диалог с агентом" : "Чат появится после обработки программы"}</h2>
              <p>
                {canUseChat
                  ? "Задайте вопрос по документам или опишите нужное изменение."
                  : "Загрузите ZIP-пакет и дождитесь распаковки и парсинга документов. Для работы с правками потребуется подтверждённая версия."}
              </p>
              {canUseChat && (
                <div className="quick-prompts" aria-label="Примеры запросов">
                  <button type="button" onClick={() => onSend("Покажи структуру программы")}>Покажи структуру программы</button>
                  <button type="button" onClick={() => onSend("Проверь результаты обучения")}>Проверь результаты обучения</button>
                </div>
              )}
            </div>
          ) : (
            messages.map((message) => (
              <article className={`chat-message chat-message--${message.role}`} key={message.id}>
                {message.role === "agent" && <div className="agent-mark" aria-hidden="true">ИИ</div>}
                <div className="message-column">
                  <div className="message-meta">{message.role === "agent" ? "Агент" : "Вы"}</div>
                  <div className={`message-bubble message-bubble--${message.role}`}>{message.text}</div>

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

        {!canUseChat && (
          <p className="chat-connection-note">
            Подключение к API чата ещё не настроено. Этот экран пока показывает состояния интерфейса.
          </p>
        )}

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
            rows={2}
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            placeholder={isConfirmed ? "Опишите, что нужно изменить, или задайте вопрос…" : "Задайте вопрос о программе…"}
            disabled={!canUseChat}
          />
          <button className="chat-send-button" disabled={!canUseChat || !draft.trim()} type="submit">
            Отправить
          </button>
          <span className="composer-hint">
            {isConfirmed ? "Правки будут показаны планом до применения." : "До подтверждения версии чат работает в режиме вопросов."}
          </span>
        </form>
      </section>

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
  const hasDemoPreview = isChatPreview || previewMode === "reconciliation";
  const processing = useImportJob();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [documents, setDocuments] = useState<ProgramDocument[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [notice, setNotice] = useState("");
  const [isDemoMode, setIsDemoMode] = useState(hasDemoPreview);
  const [activeScreen, setActiveScreen] = useState<AppScreen>(isChatPreview ? "Чат" : "Программа");
  const [parsedProgram, setParsedProgram] = useState<ParsedProgram | null>(() =>
    hasDemoPreview ? demoProgram : null,
  );
  const chatPreviewChoices = isChatPreview
    ? Object.fromEntries(demoProgram.findings.map((finding) => [finding.id, finding.choices[0]?.id ?? ""]))
    : {};
  const [selectedChoices, setSelectedChoices] = useState<Record<string, string>>(chatPreviewChoices);
  const [appliedChoices, setAppliedChoices] = useState<Record<string, string>>(chatPreviewChoices);
  const [isConfirmed, setIsConfirmed] = useState(isChatPreview);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>(hasDemoPreview ? demoChatMessages : []);
  const [chatDraft, setChatDraft] = useState("");
  const [selectedSourceRef, setSelectedSourceRef] = useState<SourceRef | null>(null);
  const sourceReturnFocusRef = useRef<HTMLElement | null>(null);
  const hasReconciliation = parsedProgram !== null;
  const unresolvedCount = parsedProgram?.findings.filter((finding) => {
    const appliedChoice = appliedChoices[finding.id];
    const selectedChoice = selectedChoices[finding.id];
    return !appliedChoice || (selectedChoice !== undefined && selectedChoice !== appliedChoice);
  }).length ?? 0;
  const hasUploadedFiles = documents.length > 0 || processing.job !== null;
  const uploadNotice = processing.notice || notice;

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
      setParsedProgram(null);
      setIsDemoMode(false);
      setSelectedChoices({});
      setAppliedChoices({});
      setIsConfirmed(false);
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

  function applyChoice(findingId: string) {
    const choiceId = selectedChoices[findingId];
    if (!choiceId) return;
    setAppliedChoices((current) => ({ ...current, [findingId]: choiceId }));
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

  const pageStatus = isDemoMode
    ? "Предпросмотр"
    : isConfirmed
      ? "Версия подтверждена"
      : hasReconciliation
        ? "Готова к сверке"
        : hasUploadedFiles
          ? "Пакет загружен"
          : "Ожидает загрузки";

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Система проверки документов</div>
        <div className="user-avatar" aria-hidden="true">АК</div>
      </header>

      <div className="workspace">
        <aside className="sidebar" aria-label="Разделы приложения">
          <nav className="side-nav">
            {navigation.map((item) => (
              <button
                type="button"
                key={item}
                className={`nav-item${item === activeScreen ? " is-active" : ""}`}
                aria-current={item === activeScreen ? "page" : undefined}
                aria-disabled={item === "Выгрузка" ? true : undefined}
                disabled={item === "Выгрузка"}
                title={item === "Выгрузка" ? "Экран будет подготовлен отдельно" : undefined}
                onClick={() => {
                  if (item !== "Выгрузка") setActiveScreen(item);
                }}
              >
                {item}
              </button>
            ))}
          </nav>
        </aside>

        <main className="main-content">
          <div className="content-column">
            {activeScreen === "Чат" ? (
              <ChatScreen
                programName={parsedProgram?.name ?? "Образовательная программа"}
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

            {isDemoMode && (
              <div className="demo-banner" role="status">
                Предпросмотр экрана: данные ниже демонстрационные и не получены из архива.
              </div>
            )}

            {parsedProgram && (
              <section className="program-card card" aria-label="Название программы">
                <div>
                  <h2>{parsedProgram.name}</h2>
                  <p>{parsedProgram.summary}</p>
                </div>
                <span className="version-label">{isConfirmed ? "v1 · подтверждена" : "Черновик версии v1"}</span>
              </section>
            )}

            <section className="documents-section" aria-labelledby="documents-title">
              <div className="section-heading">
                <h2 id="documents-title">Документы программы</h2>
                <span className="file-count">
                  {hasUploadedFiles
                    ? "1 ZIP-архив"
                    : isDemoMode && parsedProgram
                      ? `Демо-пакет · ${formatFileCount(parsedProgram.documentCount)}`
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
                  <strong>Перетащите сюда ZIP-архив</strong>
                  <span>Название программы и результаты появятся после распаковки и парсинга документов.</span>
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
                  Выбрать архив
                </button>
                {uploadNotice && <p className="upload-notice" role="status">{uploadNotice}</p>}
              </div>
            </section>

            {(hasUploadedFiles || hasReconciliation) && (
              <section className="processing-card card" aria-labelledby="processing-title">
                <div className="processing-heading">
                  <div>
                    <h2 id="processing-title">Сверка программы</h2>
                    <p className="processing-subtitle">
                      {parsedProgram
                        ? `Обработано ${formatFileCount(parsedProgram.documentCount)} · разрешите расхождения и подтвердите версию`
                        : "Проверка начнётся после загрузки, распаковки и парсинга архива."}
                    </p>
                  </div>
                  {parsedProgram ? (
                    <span className={`status-pill ${unresolvedCount ? "status-pill--warning" : "status-pill--green"}`}>
                      {unresolvedCount ? `${unresolvedCount} требуют решения` : "Все расхождения разрешены"}
                    </span>
                  ) : (
                    <span className="status-pill status-pill--muted">Ожидает обработки</span>
                  )}
                </div>

                {parsedProgram ? (
                  <>
                    <div className="reconciliation-summary" aria-label="Сводка проверки">
                      <div className="summary-metric summary-metric--conflicts">
                        <strong>{parsedProgram.findings.length}</strong>
                        <span>расхождения</span>
                      </div>
                      <div className="summary-metric summary-metric--errors">
                        <strong>{parsedProgram.issues.filter((issue) => issue.severity === "error").length}</strong>
                        <span>ошибки</span>
                      </div>
                      <div className="summary-metric summary-metric--warnings">
                        <strong>{parsedProgram.issues.filter((issue) => issue.severity === "warning").length}</strong>
                        <span>предупреждения</span>
                      </div>
                      <div className="summary-metric summary-metric--info">
                        <strong>{parsedProgram.issues.filter((issue) => issue.severity === "info").length}</strong>
                        <span>информация</span>
                      </div>
                    </div>

                    <div className="finding-list">
                      {parsedProgram.findings.map((finding, index) => {
                        const appliedChoiceId = appliedChoices[finding.id];
                        const appliedChoice = finding.choices.find((choice) => choice.id === appliedChoiceId);
                        const selectedChoiceId = selectedChoices[finding.id] ?? appliedChoiceId ?? "";
                        const isResolved = Boolean(appliedChoice && selectedChoiceId === appliedChoice.id);

                        return (
                          <article className={`finding-card${isResolved ? " is-resolved" : ""}`} key={finding.id}>
                            <div className="finding-topline">
                              <span className="finding-label">Расхождение {index + 1} из {parsedProgram.findings.length}</span>
                              <span className={`finding-status${isResolved ? " finding-status--resolved" : ""}`}>
                                {isResolved ? "Решено" : "Нужно решение"}
                              </span>
                            </div>
                            <h3>{finding.title}</h3>
                            <p className="finding-description">{finding.description}</p>
                            <fieldset className="choice-list">
                              <legend>Выберите верное значение</legend>
                              {finding.choices.map((choice) => (
                                <label className={`choice-card${selectedChoiceId === choice.id ? " is-selected" : ""}`} key={choice.id}>
                                  <input
                                    type="radio"
                                    name={`choice-${finding.id}`}
                                    value={choice.id}
                                    checked={selectedChoiceId === choice.id}
                                    disabled={isConfirmed}
                                    onChange={() => setSelectedChoices((current) => ({ ...current, [finding.id]: choice.id }))}
                                  />
                                  <span className="choice-content">
                                    <span className="choice-value">{choice.value}</span>
                                    <span className="choice-source">{choice.source}</span>
                                    <span className="choice-location">{choice.location}</span>
                                  </span>
                                </label>
                              ))}
                            </fieldset>
                            <div className="finding-footer">
                              {isResolved && appliedChoice ? (
                                <p className="resolution-note">В версии сохранено: <strong>{appliedChoice.value}</strong> · {appliedChoice.source}</p>
                              ) : (
                                <span className="finding-help">Выбор будет применён к версии программы.</span>
                              )}
                              <button
                                className="secondary-button"
                                disabled={isConfirmed || !selectedChoiceId || selectedChoiceId === appliedChoiceId}
                                onClick={() => applyChoice(finding.id)}
                                type="button"
                              >
                                {appliedChoice ? "Обновить выбор" : "Применить выбор"}
                              </button>
                            </div>
                          </article>
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

                    <div className="confirm-version">
                      <div className="confirm-copy">
                        <strong>{isConfirmed ? "Версия программы подтверждена" : "Готовы зафиксировать версию?"}</strong>
                        <span>
                          {isConfirmed
                            ? "Результат подтверждения сохранён в этом предпросмотре."
                            : unresolvedCount
                              ? `Сначала примените решение для ${unresolvedCount} ${unresolvedCount === 1 ? "расхождения" : "расхождений"}.`
                              : "После подтверждения эта версия станет основой для работы с программой."}
                        </span>
                      </div>
                      <button
                        className="confirm-button"
                        disabled={unresolvedCount > 0 || isConfirmed}
                        onClick={() => setIsConfirmed(true)}
                        type="button"
                      >
                        {isConfirmed ? "Версия подтверждена" : "Подтвердить версию"}
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
                    <p className="processing-caption">
                      Название программы, извлечённые данные и расхождения появятся после завершения обработки архива.
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
