import { useEffect, useState } from "react";

type GenerationState =
  | { kind: "ready" }
  | { kind: "running"; progress: number }
  | { kind: "completed"; packageVersion: number; generatedAt: string };

type GenerationScreenProps = {
  isConfirmed: boolean;
  isDemoMode: boolean;
  versionNumber: number;
};

const packageDocuments = [
  { name: "Учебный план", detail: "DOCX · 1 документ" },
  { name: "РПД и РПП", detail: "DOCX · 50 документов" },
  { name: "ЦКМВ", detail: "DOCX · 1 документ" },
  { name: "Характеристика программы", detail: "DOCX · 1 документ" },
] as const;

function GenerationScreen({ isConfirmed, isDemoMode, versionNumber }: GenerationScreenProps) {
  const [generation, setGeneration] = useState<GenerationState>(() => (
    isDemoMode && isConfirmed
      ? { kind: "completed", packageVersion: versionNumber, generatedAt: "Демонстрационный результат" }
      : { kind: "ready" }
  ));
  const [notice, setNotice] = useState("");
  const isGenerating = generation.kind === "running";

  useEffect(() => {
    if (!isGenerating) return;

    const progressTimers = [
      window.setTimeout(() => {
        setGeneration((current) => current.kind === "running" ? { ...current, progress: 68 } : current);
      }, 450),
      window.setTimeout(() => {
        setGeneration((current) => current.kind === "running" ? { ...current, progress: 91 } : current);
      }, 1100),
    ];
    const completionTimer = window.setTimeout(() => {
      setGeneration({ kind: "completed", packageVersion: versionNumber, generatedAt: "Только что" });
    }, 2100);

    return () => {
      progressTimers.forEach(window.clearTimeout);
      window.clearTimeout(completionTimer);
    };
  }, [isGenerating, versionNumber]);

  function startGeneration() {
    if (!isConfirmed || isGenerating) return;
    setNotice("");
    if (!isDemoMode) {
      setNotice("Запуск генерации станет доступен после подключения API.");
      return;
    }
    setGeneration({ kind: "running", progress: 34 });
  }

  function showPreviewNotice() {
    setNotice("Скачивание будет подключено вместе с API генерации.");
  }

  const isCompleted = generation.kind === "completed";
  const progress = generation.kind === "running" ? generation.progress : isCompleted ? 100 : 0;
  const packageVersion = generation.kind === "completed" ? generation.packageVersion : versionNumber;
  const generatedAt = generation.kind === "completed" ? generation.generatedAt : "";
  const generationStatus = !isConfirmed
    ? "Нужна подтверждённая версия"
    : isGenerating
      ? "Формируем пакет"
      : isCompleted
        ? "Пакет готов"
        : "Готов к запуску";

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
            {isConfirmed
              ? "Сформируем документы на основе подтверждённой версии программы."
              : "Сначала подтвердите версию программы на экране «Программа»."}
          </p>
        </div>
        <div className="generation-summary-action">
          <span className={"generation-state" + (isGenerating ? " is-running" : "")} role="status">
            <span aria-hidden="true" />
            {generationStatus}
          </span>
          <button
            className="primary-button generation-start-button"
            type="button"
            disabled={!isConfirmed || isGenerating}
            onClick={startGeneration}
          >
            {isGenerating ? "Собираем пакет…" : isCompleted ? "Сформировать новый пакет" : "Сформировать пакет"}
          </button>
        </div>
      </section>

      <div className="generation-layout">
        <div className="generation-main-column">
          <section className="generation-section card" aria-labelledby="generation-documents-title">
            <div className="generation-section-heading">
              <div>
                <h2 id="generation-documents-title">Состав пакета</h2>
                <p>Основные документы программы · 53 файла</p>
              </div>
              <span className="generation-count">4 типа</span>
            </div>

            {isGenerating && (
              <div className="generation-progress-block" role="status">
                <div className="generation-progress-copy">
                  <span>Подготовка документов</span>
                  <strong>{progress}%</strong>
                </div>
                <progress max={100} value={progress} aria-label="Прогресс генерации пакета" />
                <span className="generation-progress-hint">
                  {progress < 68 ? "Собираем изменённые разделы…" : progress < 91 ? "Проверяем комплектность…" : "Создаём архив…"}
                </span>
              </div>
            )}

            <ul className="generation-document-list">
              {packageDocuments.map((document, index) => {
                const documentProgress = generation.kind === "running"
                  ? Math.max(0, Math.min(100, progress - index * 18))
                  : isCompleted ? 100 : 0;
                const isDocumentReady = documentProgress === 100;
                const isDocumentActive = generation.kind === "running" && documentProgress > 0 && !isDocumentReady;

                return (
                  <li className="generation-document-row" key={document.name}>
                    <span className="generation-file-icon" aria-hidden="true">DOCX</span>
                    <span className="generation-document-copy">
                      <strong>{document.name}</strong>
                      <span>{document.detail}</span>
                    </span>
                    <span className={"generation-document-status" + (isDocumentReady ? " is-ready" : "")}>
                      {isDocumentReady ? "Готово" : isDocumentActive ? "Готовим…" : isGenerating ? "В очереди" : "Ожидает"}
                    </span>
                  </li>
                );
              })}
            </ul>
            <p className="generation-note">
              В пакет войдут учебный план, РПД и РПП, ЦКМВ и характеристика программы.
            </p>
          </section>

          <section className="generation-section card" aria-labelledby="generation-metrics-title">
            <div className="generation-section-heading">
              <div>
                <h2 id="generation-metrics-title">Сводка проверки</h2>
                <p>По текущей версии программы</p>
              </div>
              <span className="generation-report-status">{isDemoMode ? "Демо-сводка" : "Нет данных"}</span>
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
            ) : (
              <p className="generation-empty-metrics">
                Сводка появится после подключения результатов проверки текущей версии.
              </p>
            )}
          </section>
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
                  <span>14,2 МБ · {generatedAt}</span>
                </div>
                <button className="generation-download-button" type="button" onClick={showPreviewNotice}>
                  Скачать пакет
                </button>
                <div className="generation-report-links">
                  <span>Протокол проверки</span>
                  <button type="button" onClick={showPreviewNotice}>DOCX</button>
                  <button type="button" onClick={showPreviewNotice}>PDF</button>
                </div>
              </>
            ) : (
              <div className="generation-empty-result">
                <strong>{isGenerating ? "Архив появится после сборки" : "Пока нет готового архива"}</strong>
                <span>{isConfirmed ? "Запустите генерацию, чтобы собрать пакет и протокол." : "Подтвердите версию программы, чтобы продолжить."}</span>
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
            {isCompleted ? (
              <ol className="generation-history-list">
                <li className="is-current">
                  <span className="generation-history-dot" aria-hidden="true" />
                  <span><strong>Версия {packageVersion}</strong><small>{generatedAt}</small></span>
                  <span className="generation-history-current">Текущая</span>
                </li>
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
