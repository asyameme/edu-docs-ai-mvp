import { useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { ImportProgress } from "./ImportProgress";
import { useImportJob } from "./useImportJob";

type DocumentStatus = "Загружено";

type ProgramDocument = {
  name: string;
  status: DocumentStatus;
};

type ReconciliationFinding = {
  title: string;
  planValue: string;
  rpdValue: string;
  sources: string;
  options: string[];
};

type ParsedProgram = {
  name: string;
  summary: string;
  documentCount: number;
  errorCount: number;
  warningCount: number;
  finding: ReconciliationFinding | null;
};

const navigation = ["Программа", "Чат", "Выгрузка"];

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

function formatCount(count: number, forms: [string, string, string]) {
  const lastTwoDigits = count % 100;
  const lastDigit = count % 10;
  if (lastTwoDigits >= 11 && lastTwoDigits <= 14) return `${count} ${forms[2]}`;
  if (lastDigit === 1) return `${count} ${forms[0]}`;
  if (lastDigit >= 2 && lastDigit <= 4) return `${count} ${forms[1]}`;
  return `${count} ${forms[2]}`;
}

function App() {
  const processing = useImportJob();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [documents, setDocuments] = useState<ProgramDocument[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [notice, setNotice] = useState("");
  const [parsedProgram, setParsedProgram] = useState<ParsedProgram | null>(null);
  const [selectedResolution, setSelectedResolution] = useState<string | null>(null);
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
      setSelectedResolution(null);
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

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Система проверки документов</div>
        <div className="user-avatar" aria-hidden="true">
          АК
        </div>
      </header>

      <div className="workspace">
        <aside className="sidebar" aria-label="Разделы приложения">
          <nav className="side-nav">
            {navigation.map((item) => (
              <div
                key={item}
                className={`nav-item${item === "Программа" ? " is-active" : ""}`}
                aria-current={item === "Программа" ? "page" : undefined}
              >
                {item}
              </div>
            ))}
          </nav>
        </aside>

        <main className="main-content">
          <div className="content-column">
            <section className="page-heading">
              <div>
                <h1>Программа</h1>
                <p className="eyebrow">Пакет документов, обработка и найденные расхождения</p>
              </div>
              <span
                className={`status-pill ${hasUploadedFiles ? "status-pill--green" : "status-pill--muted"}`}
              >
                {hasUploadedFiles ? "Пакет загружен" : "Ожидает загрузки"}
              </span>
            </section>

            {parsedProgram && (
              <section className="program-card card" aria-label="Название программы">
                <div>
                  <h2>{parsedProgram.name}</h2>
                  <p>{parsedProgram.summary}</p>
                </div>
              </section>
            )}

            <section className="documents-section" aria-labelledby="documents-title">
              <div className="section-heading">
                <h2 id="documents-title">Документы программы</h2>
                <span className="file-count">
                  {hasUploadedFiles ? "1 ZIP-архив" : "Архив не загружен"}
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
                  <span>Загрузите архив со всем пакетом документов. Обработка начнётся после загрузки.</span>
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
                  Выбрать файлы
                </button>
                {uploadNotice && <p className="upload-notice" role="status">{uploadNotice}</p>}
              </div>
            </section>

            {hasUploadedFiles && (
              <section className="processing-card card" aria-labelledby="processing-title">
                <div className="processing-heading">
                  <h2 id="processing-title">Обработка и сверка</h2>
                  {parsedProgram ? (
                    <span className="issue-count-pill">
                      {formatCount(parsedProgram.errorCount, ["ошибка", "ошибки", "ошибок"])}
                      {" · "}
                      {formatCount(parsedProgram.warningCount, [
                        "предупреждение",
                        "предупреждения",
                        "предупреждений",
                      ])}
                    </span>
                  ) : (
                    <span className="status-pill status-pill--muted">Архив загружен</span>
                  )}
                </div>

                {parsedProgram ? (
                  <>
                    <p className="finding-summary">
                      Все {formatFileCount(parsedProgram.documentCount)} обработаны. Проверьте
                      расхождения перед подтверждением.
                    </p>
                    {parsedProgram.finding && (
                      <article className="finding-card">
                        <span className="finding-label">Нужно решить</span>
                        <h3>{parsedProgram.finding.title}</h3>
                        <p className="finding-values">
                          План: {parsedProgram.finding.planValue}
                          <span aria-hidden="true"> · </span>
                          РПД: {parsedProgram.finding.rpdValue}
                        </p>
                        <p className="finding-sources">
                          Источники: {parsedProgram.finding.sources}
                        </p>
                        <div className="resolution-actions" role="group" aria-label="Решение по расхождению">
                          {parsedProgram.finding.options.map((option) => (
                            <button
                              className={`resolution-button${selectedResolution === option ? " is-selected" : ""}`}
                              key={option}
                              onClick={() => setSelectedResolution(option)}
                              type="button"
                            >
                              {option}
                            </button>
                          ))}
                        </div>
                      </article>
                    )}
                  </>
                ) : (
                  <>
                    <ul className="document-list">
                      {documents.map((document) => (
                        <li className="document-row" key={document.name}>
                          <span className="document-name">{document.name}</span>
                          <span className="status-pill status-pill--green document-status">
                            {document.status}
                          </span>
                        </li>
                      ))}
                    </ul>
                    {processing.job && (
                      <ImportProgress
                        programId={processing.programId}
                        job={processing.job}
                        busy={processing.busy}
                        onRetry={processing.retry}
                      />
                    )}
                    <p className="processing-caption">
                      Название программы и результаты сверки появятся после распаковки и парсинга архива.
                    </p>
                  </>
                )}
              </section>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
