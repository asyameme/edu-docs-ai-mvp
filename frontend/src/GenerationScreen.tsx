import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type CheckReport,
  type GenerationJob,
  type PackageComposition,
  type RuleCoverage,
  type VersionRead,
} from "./api";
import { DiagnosticList } from "./DiagnosticList";
import type { Diagnostic } from "./diagnostics";

export type GenerationReview =
  | { kind: "confirmed"; versionId: string }
  | { kind: "ready"; confirm: () => Promise<VersionRead | null> }
  | { kind: "blocked"; reason: string };

type GenerationScreenProps = {
  programId: string;
  review: GenerationReview;
  versionNumber: number;
  isDemoMode: boolean;
  checkReport: CheckReport | null;
  issues: Diagnostic[];
  freshIssueIds: string[] | null;
  reviewNotice: string;
  acceptedWarnings: number;
};

type DemoGeneration =
  | { kind: "ready" }
  | { kind: "running"; progress: number }
  | { kind: "completed"; packageVersion: number; generatedAt: string };

const TYPE_LABELS: Record<string, string> = {
  CURRICULUM: "Учебный план",
  RPD: "РПД",
  RPP: "РПП",
  CKMV: "ЦКМВ",
  CHARACTERISTIC: "Характеристика программы",
};
const TYPE_ORDER = ["CURRICULUM", "RPD", "RPP", "CKMV", "CHARACTERISTIC"];

// Состав демонстрационного макета; в рабочем режиме его возвращает API.
const demoDocuments = [
  { name: TYPE_LABELS.CURRICULUM, detail: "DOCX · 1 документ" },
  { name: "РПД и РПП", detail: "DOCX · 50 документов" },
  { name: TYPE_LABELS.CKMV, detail: "DOCX · 1 документ" },
  { name: TYPE_LABELS.CHARACTERISTIC, detail: "DOCX · 1 документ" },
];
const matrixRow = { name: "Матрица компетенций", detail: "XLSX · строится по данным программы" };

function formatCount(count: number, one: string, few: string, many: string) {
  const lastTwoDigits = count % 100;
  const lastDigit = count % 10;
  if (lastTwoDigits >= 11 && lastTwoDigits <= 14) return `${count} ${many}`;
  if (lastDigit === 1) return `${count} ${one}`;
  if (lastDigit >= 2 && lastDigit <= 4) return `${count} ${few}`;
  return `${count} ${many}`;
}

function formatBytes(size: number | null) {
  if (size === null) return "";
  const megabytes = size / (1024 * 1024);
  if (megabytes < 1) return `${Math.max(1, Math.round(size / 1024))} КБ`;
  return `${megabytes.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`;
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function countByStatus(coverage: RuleCoverage[]) {
  const counts: Record<RuleCoverage["status"], number> = {
    passed: 0, failed: 0, needs_review: 0, skipped: 0, not_applicable: 0,
  };
  for (const item of coverage) counts[item.status] += 1;
  return counts;
}

function packageDocuments(composition: PackageComposition | null) {
  if (!composition) return null;
  const groups = new Map<string, number>();
  for (const document of composition.documents) {
    groups.set(document.doc_type, (groups.get(document.doc_type) ?? 0) + 1);
  }
  const known = TYPE_ORDER.filter((type) => groups.has(type)).map((type) => ({
    name: TYPE_LABELS[type],
    detail: `DOCX · ${formatCount(groups.get(type) ?? 0, "документ", "документа", "документов")}`,
  }));
  const other = [...groups.keys()].filter((type) => !TYPE_ORDER.includes(type)).map((type) => ({
    name: TYPE_LABELS[type] ?? type,
    detail: `DOCX · ${formatCount(groups.get(type) ?? 0, "документ", "документа", "документов")}`,
  }));
  return [...known, ...other];
}

function GenerationScreen({
  programId,
  review,
  versionNumber,
  isDemoMode,
  checkReport,
  issues,
  freshIssueIds,
  reviewNotice,
  acceptedWarnings,
}: GenerationScreenProps) {
  const isConfirmed = review.kind === "confirmed";
  const confirmedVersionId = review.kind === "confirmed" ? review.versionId : "";
  const [demo, setDemo] = useState<DemoGeneration>(() => (
    isDemoMode && isConfirmed
      ? { kind: "completed", packageVersion: versionNumber, generatedAt: "Демонстрационный результат" }
      : { kind: "ready" }
  ));
  const [composition, setComposition] = useState<PackageComposition | null>(null);
  const [job, setJob] = useState<GenerationJob | null>(null);
  const [history, setHistory] = useState<GenerationJob[]>([]);
  const [starting, setStarting] = useState(false);
  const [notice, setNotice] = useState("");
  const base = `/api/v1/programs/${programId}/generation`;

  const isRunning = isDemoMode
    ? demo.kind === "running"
    : Boolean(job && (job.status === "pending" || job.status === "processing"));
  const isCompleted = isDemoMode ? demo.kind === "completed" : job?.status === "completed";

  useEffect(() => {
    if (isDemoMode || !confirmedVersionId) return;
    let active = true;
    // Состав и история загружаются независимо: отказ одной не должен скрывать другую.
    void api<PackageComposition>(`${base}/composition`).then((pack) => {
      if (active) setComposition(pack);
    }).catch((error: unknown) => {
      if (active) setNotice(errorMessage(error, "Не удалось загрузить состав пакета."));
    });
    void api<GenerationJob[]>(`${base}?limit=20&offset=0`).then((jobs) => {
      if (!active) return;
      setHistory(jobs);
      const own = jobs.filter((item) => item.version_id === confirmedVersionId);
      setJob((current) => current?.version_id === confirmedVersionId ? current : own[0] ?? null);
    }).catch((error: unknown) => {
      if (active) setNotice(errorMessage(error, "Не удалось загрузить историю генерации."));
    });
    return () => { active = false; };
  }, [isDemoMode, confirmedVersionId, base]);

  useEffect(() => {
    if (isDemoMode || !job || !["pending", "processing"].includes(job.status)) return;
    let active = true;
    const poll = async () => {
      try {
        const next = await api<GenerationJob>(`${base}/${job.id}`);
        if (!active) return;
        setJob(next);
        setHistory((items) => [next, ...items.filter((item) => item.id !== next.id)].slice(0, 20));
        return;
      } catch (error) {
        if (active && !(error instanceof ApiError && error.status === 503)) {
          setNotice(errorMessage(error, "Не удалось получить статус генерации."));
        }
      }
      if (active) window.setTimeout(() => void poll(), 3000);
    };
    const timer = window.setTimeout(() => void poll(), 1200);
    return () => { active = false; window.clearTimeout(timer); };
  }, [isDemoMode, job, base]);

  async function startGeneration() {
    if (review.kind === "blocked" || isRunning || starting) return;
    setNotice("");
    if (isDemoMode) {
      setDemo({ kind: "running", progress: 34 });
      return;
    }
    setStarting(true);
    try {
      const version = review.kind === "ready" ? await review.confirm() : null;
      if (review.kind === "ready" && !version) return;
      setJob(await api<GenerationJob>(base, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ version_id: version?.id ?? confirmedVersionId }),
      }));
    } catch (error) {
      setNotice(errorMessage(error, "Не удалось запустить генерацию."));
    } finally {
      setStarting(false);
    }
  }

  useEffect(() => {
    if (!isDemoMode || demo.kind !== "running") return;
    const progressTimers = [
      window.setTimeout(() => {
        setDemo((current) => current.kind === "running" ? { ...current, progress: 68 } : current);
      }, 450),
      window.setTimeout(() => {
        setDemo((current) => current.kind === "running" ? { ...current, progress: 91 } : current);
      }, 1100),
    ];
    const completionTimer = window.setTimeout(() => {
      setDemo({ kind: "completed", packageVersion: versionNumber, generatedAt: "Только что" });
    }, 2100);
    return () => {
      progressTimers.forEach(window.clearTimeout);
      window.clearTimeout(completionTimer);
    };
  }, [isDemoMode, demo.kind, versionNumber]);

  const progress = demo.kind === "running" ? demo.progress : isCompleted ? 100 : 0;
  const packageVersion = isDemoMode && demo.kind === "completed" ? demo.packageVersion : job?.version_number ?? versionNumber;
  const generatedAt = isDemoMode
    ? demo.kind === "completed" ? demo.generatedAt : ""
    : job ? formatTime(job.created_at) : "";
  const documents = isDemoMode ? demoDocuments : packageDocuments(composition);
  // В пакет входят все документы версии и матрица компетенций.
  const fileCount = isDemoMode
    ? 53
    : composition ? composition.documents.length + 1 : 0;
  const failed = !isDemoMode && job?.status === "failed";
  const manualNotes = job?.notes ?? [];
  const generationStatus = !isConfirmed
    ? review.kind === "ready" ? "Проверка завершена" : "Ожидает подтверждения"
    : isRunning
      ? "Формируем пакет"
      : isCompleted
        ? (manualNotes.length ? "Пакет готов, есть правки вручную" : "Пакет готов")
        : failed
          ? "Сборка не удалась"
          : "Готов к запуску";
  const statuses = countByStatus(checkReport?.coverage ?? []);
  const changedDocuments = job?.applied.length ?? 0;
  const freshIssueIdSet = new Set(freshIssueIds ?? []);
  const freshIssues = freshIssueIds === null ? [] : issues.filter((issue) => freshIssueIdSet.has(issue.id));
  const warningCount = checkReport?.issues.filter((issue) => issue.severity === "warning").length ?? 0;
  const freshWarningCount = freshIssues.filter((issue) => issue.severity === "warning").length;
  const hasFreshWarnings = freshWarningCount > 0;
  const showIssues = !isDemoMode && freshIssues.length > 0;
  const freshnessPending = !isDemoMode && review.kind === "ready" && Boolean(checkReport) && freshIssueIds === null;
  const startButton = (
    <button
      className="primary-button generation-start-button"
      type="button"
      disabled={review.kind === "blocked" || isRunning || starting || freshnessPending}
      onClick={() => { void startGeneration(); }}
    >
      {starting
        ? isConfirmed ? "Запускаем сборку…" : "Подтверждаем версию…"
        : isRunning
          ? "Собираем пакет…"
          : freshnessPending
            ? "Обновляем замечания…"
          : isConfirmed
            ? isCompleted ? "Сформировать новый пакет" : "Сформировать пакет"
            : hasFreshWarnings ? "Принять новые замечания и сформировать пакет" : "Подтвердить и сформировать пакет"}
    </button>
  );

  return (
    <>
      <section className="page-heading">
        <div>
          <h1>Генерация</h1>
          <p className="eyebrow">Сборка и выгрузка документов программы</p>
        </div>
        <span className={"status-pill " + (isConfirmed ? "status-pill--green" : "status-pill--muted")}>
          Версия {versionNumber} · {isConfirmed ? "подтверждена" : "черновик"}
        </span>
      </section>

      <section className="generation-summary card" aria-labelledby="generation-summary-title">
        <div className="generation-summary-copy">
          <span className="generation-overline">Пакет документов · версия {versionNumber}</span>
          <h2 id="generation-summary-title">
            {isCompleted ? "Пакет документов собран" : "Подготовьте новый пакет документов"}
          </h2>
          <p>
            {review.kind === "blocked"
              ? review.reason
              : isConfirmed
                ? "Сформируем документы на основе подтверждённой версии программы."
                : hasFreshWarnings
                  ? "Просмотрите оставшиеся замечания перед сборкой пакета."
                  : "Проверка завершена, версию можно подтвердить и собрать пакет."}
          </p>
          {reviewNotice && <p className="generation-notice" role="status">{reviewNotice}</p>}
        </div>
        <div className="generation-summary-action">
          <span className={"generation-state" + (isRunning ? " is-running" : "")} role="status">
            <span aria-hidden="true" />
            {generationStatus}
          </span>
          {(!showIssues || isConfirmed) && startButton}
        </div>
      </section>

      {showIssues && (
        <section className="generation-section generation-review card" aria-labelledby="generation-issues-title">
          <div className="generation-section-heading">
            <div>
              <h2 id="generation-issues-title">{isConfirmed ? "Новые замечания проверки" : "Новые замечания"}</h2>
              <p>Показаны замечания, которых не было в предыдущей подтверждённой проверке.</p>
            </div>
          </div>
          <DiagnosticList items={freshIssues} />
          {!isConfirmed && (
            <div className="generation-review-action">
              {hasFreshWarnings && review.kind !== "blocked" && (
                <p>Новые предупреждения: {freshWarningCount}. Всего в текущей проверке предупреждений: {warningCount}; ранее принятые здесь не повторяются.</p>
              )}
              {startButton}
            </div>
          )}
        </section>
      )}

      {!isDemoMode && checkReport && freshnessPending && (
        <p className="generation-note" role="status">Сверяем результаты с предыдущей подтверждённой проверкой…</p>
      )}
      {!isDemoMode && checkReport && freshIssueIds !== null && freshIssues.length === 0 && (
        <section className="generation-section generation-review card" aria-label="Новые замечания">
          <p className="generation-empty-metrics">Новых замечаний нет. Все замечания текущей проверки уже встречались в предыдущей подтверждённой версии или отсутствуют.</p>
        </section>
      )}

      <div className="generation-layout">
        <div className="generation-main-column">
          <section className="generation-section card" aria-labelledby="generation-documents-title">
            <div className="generation-section-heading">
              <div>
                <h2 id="generation-documents-title">Состав пакета</h2>
                <p>
                  {fileCount
                    ? `${formatCount(fileCount, "файл", "файла", "файлов")} в пакете: ${formatCount(fileCount - 1, "документ DOCX", "документа DOCX", "документов DOCX")} + 1 создаваемая матрица XLSX`
                    : isConfirmed ? "Состав определяется по подтверждённой версии" : "Состав появится после подтверждения версии"}
                </p>
              </div>
              <span className="generation-count">
                {documents ? formatCount(documents.length + 1, "тип", "типа", "типов") : "—"}
              </span>
            </div>

            {isRunning && (
              <div className="generation-progress-block" role="status">
                <div className="generation-progress-copy">
                  <span>{isDemoMode ? "Подготовка документов" : job?.status === "pending" ? "Задание в очереди" : "Собираем пакет"}</span>
                  {isDemoMode && <strong>{progress}%</strong>}
                </div>
                <progress max={100} value={isDemoMode ? progress : undefined} aria-label="Прогресс генерации пакета" />
                <span className="generation-progress-hint">
                  {isDemoMode
                    ? (progress < 68 ? "Собираем изменённые разделы…" : progress < 91 ? "Проверяем комплектность…" : "Создаём архив…")
                    : "На большом пакете сборка занимает одну-две минуты. Страницу можно не закрывать: результат появится здесь."}
                </span>
              </div>
            )}

            <ul className="generation-document-list">
              {(documents ?? []).map((document, index) => {
                const documentProgress = isRunning && isDemoMode
                  ? Math.max(0, Math.min(100, progress - index * 18))
                  : isCompleted ? 100 : 0;
                const isDocumentReady = documentProgress === 100;
                const isDocumentActive = isRunning && isDemoMode && documentProgress > 0 && !isDocumentReady;

                return (
                  <li className="generation-document-row" key={document.name}>
                    <span className="generation-file-icon" aria-hidden="true">DOCX</span>
                    <span className="generation-document-copy">
                      <strong>{document.name}</strong>
                      <span>{document.detail}</span>
                    </span>
                    <span className={"generation-document-status" + (isDocumentReady ? " is-ready" : "")}>
                      {isDocumentReady ? "Готово" : isDocumentActive ? "Готовим…" : isRunning ? (isDemoMode ? "В очереди" : "В пакете") : "Ожидает"}
                    </span>
                  </li>
                );
              })}
              {documents && (
                <li className="generation-document-row" key={matrixRow.name}>
                  <span className="generation-file-icon" aria-hidden="true">XLSX</span>
                  <span className="generation-document-copy">
                    <strong>{matrixRow.name}</strong>
                    <span>{matrixRow.detail}</span>
                  </span>
                  <span className={"generation-document-status" + (isCompleted ? " is-ready" : "")}>
                    {isCompleted ? "Готово" : isRunning ? "В пакете" : "Ожидает"}
                  </span>
                </li>
              )}
            </ul>
            <p className="generation-note">
              В пакет войдут учебный план, РПД и РПП, ЦКМВ и характеристика программы с внесёнными правками; матрица компетенций строится по данным подтверждённой версии.
            </p>
          </section>

          <section className="generation-section card" aria-labelledby="generation-metrics-title">
            <div className="generation-section-heading">
              <div>
                <h2 id="generation-metrics-title">Сводка проверки</h2>
                <p>По текущей версии программы</p>
              </div>
              <span className="generation-report-status">{isDemoMode ? "Демо-сводка" : checkReport ? "Результаты проверки" : "Нет данных"}</span>
            </div>
            {isDemoMode ? (
              <div className="generation-metrics">
                <article className="generation-metric">
                  <span>Покрытие компетенций</span>
                  <strong>96%</strong>
                  <div className="generation-metric-track" aria-hidden="true"><span style={{ width: "96%" }} /></div>
                </article>
                <article className="generation-metric">
                  <span>Объём программы</span>
                  <strong>238 <small>/ 240 ЗЕТ</small></strong>
                  <div className="generation-metric-track" aria-hidden="true"><span style={{ width: "99%" }} /></div>
                </article>
                <article className="generation-metric generation-metric--review">
                  <span>Замечания проверки</span>
                  <strong>12</strong>
                  <small>предупреждений в протоколе</small>
                </article>
              </div>
            ) : checkReport ? (
              <div className="generation-metrics">
                <article className="generation-metric">
                  <span>Правила пройдены</span>
                  <strong>{statuses.passed}</strong>
                  <small>из {checkReport.coverage.length} в проверке</small>
                </article>
                <article className="generation-metric">
                  <span>Правила с замечаниями</span>
                  <strong>{statuses.failed}</strong>
                  <small>{isConfirmed ? "учтены при подтверждении" : "подробности в списке замечаний"}</small>
                </article>
                <article className="generation-metric generation-metric--review">
                  <span>{isConfirmed ? "Принятые предупреждения" : "Новые предупреждения"}</span>
                  <strong>{isConfirmed ? acceptedWarnings : freshWarningCount}</strong>
                  <small>{isConfirmed ? "записей учтено" : `${warningCount} всего в текущей проверке`}</small>
                </article>
              </div>
            ) : (
              <p className="generation-empty-metrics">
                {isConfirmed ? "Результаты проверки подтверждённой версии недоступны." : "Сводка появится после проверки версии программы."}
              </p>
            )}
          </section>

          {isCompleted && (
            <section className="generation-section card" aria-labelledby="generation-report-title">
              <div className="generation-section-heading">
                <div>
                  <h2 id="generation-report-title">Отчёт о сборке</h2>
                  <p>Что изменено в документах и что осталось для ручной правки</p>
                </div>
                <span className="generation-report-status">{isDemoMode ? "Демонстрация" : "После сборки"}</span>
              </div>
              {isDemoMode ? (
                <p className="generation-note">В рабочем режиме здесь перечисляются изменённые документы и правки, которые нужно внести вручную.</p>
              ) : (
                <>
                  {manualNotes.length > 0 && (
                    <>
                      <h3 className="generation-report-title generation-report-title--warning">
                        Требуют ручной правки: {manualNotes.length}
                      </h3>
                      <ul className="generation-report-list" aria-label="Правки, не перенесённые в документы">
                        {manualNotes.map((note, index) => (
                          <li className="generation-report-item generation-report-item--warning" key={`${index}-${note}`}>{note}</li>
                        ))}
                      </ul>
                    </>
                  )}
                  {changedDocuments > 0 ? (
                    <details className="generation-report-details">
                      <summary>Изменено документов: {changedDocuments}</summary>
                      <ul className="generation-report-list" aria-label="Изменённые документы">
                        {job?.applied.map((line, index) => (
                          <li className="generation-report-item" key={`${index}-${line}`}>{line}</li>
                        ))}
                      </ul>
                    </details>
                  ) : (
                    <p className="generation-note">
                      {manualNotes.length > 0
                        ? "Автоматических замен в документах не было."
                        : "Документы пакета совпадают с исходными; матрица компетенций построена по данным программы."}
                    </p>
                  )}
                </>
              )}
            </section>
          )}
        </div>

        <aside className="generation-side-column" aria-label="Результаты генерации">
          <section className="generation-result card" aria-labelledby="generation-result-title">
            <div className="generation-section-heading">
              <div>
                <h2 id="generation-result-title">Результат</h2>
                <p>Текущая генерация</p>
              </div>
              <span className="generation-result-mark" aria-hidden="true">ZIP</span>
            </div>
            {isCompleted ? (
              <>
                <div className="generation-package">
                  <strong>edu_package_v{packageVersion}.zip</strong>
                  <span>{isDemoMode ? "14,2 МБ" : formatBytes(job?.size_bytes ?? null)} · {generatedAt}</span>
                </div>
                {isDemoMode ? (
                  <button className="generation-download-button" type="button" onClick={() => setNotice("В рабочем режиме здесь скачивается собранный архив.")}>
                    Скачать пакет
                  </button>
                ) : (
                  <a
                    className="generation-download-button"
                    href={`${base}/${job?.id}/download`}
                    download={`edu_package_v${packageVersion}.zip`}
                  >
                    Скачать пакет
                  </a>
                )}
              </>
            ) : (
              <div className="generation-empty-result">
                <strong>{isRunning ? "Архив появится после сборки" : failed ? "Пакет не собран" : "Пока нет готового архива"}</strong>
                <span>{isConfirmed ? "Запустите генерацию, чтобы собрать пакет." : "Подтвердите версию программы, чтобы продолжить."}</span>
              </div>
            )}
            {notice && <p className="generation-notice" role="status">{notice}</p>}
          </section>

          <section className="generation-history card" aria-labelledby="generation-history-title">
            <div className="generation-section-heading">
              <div>
                <h2 id="generation-history-title">История</h2>
                <p>Пакеты по версиям программы</p>
              </div>
            </div>
            {isDemoMode ? (
              isCompleted ? (
                <ol className="generation-history-list">
                  <li className="is-current">
                    <span className="generation-history-dot" aria-hidden="true" />
                    <span><strong>Версия {packageVersion}</strong><small>{generatedAt}</small></span>
                    <span className="generation-history-current">Текущая</span>
                  </li>
                </ol>
              ) : (
                <p className="generation-history-empty">Здесь появятся созданные пакеты.</p>
              )
            ) : history.length > 0 ? (
              <ol className="generation-history-list">
                {history.map((item) => (
                  <li className={item.id === job?.id ? "is-current" : ""} key={item.id}>
                    <span className="generation-history-dot" aria-hidden="true" />
                    <span>
                      <strong>Версия {item.version_number ?? versionNumber}</strong>
                      <small>{formatTime(item.created_at)}{item.status === "completed" && item.size_bytes !== null ? ` · ${formatBytes(item.size_bytes)}` : ""}</small>
                    </span>
                    {item.status === "completed" ? (
                      <a
                        className="generation-history-download"
                        href={`${base}/${item.id}/download`}
                        download={`edu_package_v${item.version_number ?? versionNumber}.zip`}
                      >
                        Скачать
                      </a>
                    ) : (
                      <span className="generation-history-current">{item.status === "failed" ? "Не собран" : "Формируется"}</span>
                    )}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="generation-history-empty">Здесь появятся созданные пакеты.</p>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}

export { GenerationScreen };
