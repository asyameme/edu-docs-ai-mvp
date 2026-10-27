import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { api, ApiError, loadSession, saveSession, type ImportJob } from "./api";

const labels = {
  pending: "Ожидает обработки", processing: "Обрабатывается", completed: "Обработано",
  partial: "Обработано частично", failed: "Ошибка обработки",
};

function App() {
  const initial = useRef(loadSession());
  const input = useRef<HTMLInputElement>(null);
  const busyRef = useRef(false);
  const [programId, setProgramId] = useState(initial.current?.programId || "");
  const [jobId, setJobId] = useState(initial.current?.jobId || "");
  const [job, setJob] = useState<ImportJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!programId || !jobId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let stream: EventSource | undefined;
    const connect = async () => {
      try {
        const next = await api<ImportJob>(`/api/v1/programs/${programId}/imports/${jobId}`);
        if (!active) return;
        setJob(next);
        setNotice("");
        if (["pending", "processing"].includes(next.status)) {
          stream = new EventSource(`/api/v1/programs/${programId}/imports/${jobId}/events`);
          stream.addEventListener("status", event => {
            if (!active) return;
            const updated = JSON.parse((event as MessageEvent).data) as ImportJob;
            setJob(updated);
            setNotice("");
            if (!["pending", "processing"].includes(updated.status)) stream?.close();
          });
          stream.onerror = () => {
            if (active) setNotice("Связь с сервером прервалась. Восстанавливаем состояние…");
          };
        }
      } catch (error) {
        if (!active) return;
        setNotice(error instanceof Error ? error.message : "Не удалось получить состояние задачи");
        if (!(error instanceof ApiError && error.status === 404)) timer = setTimeout(connect, 5000);
      }
    };
    void connect();
    return () => { active = false; clearTimeout(timer); stream?.close(); };
  }, [programId, jobId, job?.attempt]);

  async function addFiles(files: FileList | File[]) {
    if (busyRef.current) return;
    const selected = Array.from(files);
    if (selected.length !== 1 || !/\.zip$/i.test(selected[0].name)) {
      setNotice("Выберите один ZIP-архив с документами DOCX/XLSX.");
      return;
    }
    if (selected[0].size > 50 * 1024 * 1024) {
      setNotice("Размер архива не должен превышать 50 МиБ.");
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setNotice("Отправляем архив на сервер…");
    try {
      let target = programId;
      if (target) {
        try { await api(`/api/v1/programs/${target}`); }
        catch (error) {
          if (error instanceof ApiError && error.status === 404) target = "";
          else throw error;
        }
      }
      if (!target) {
        const created = await api<{ id: string }>("/api/v1/programs", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: selected[0].name.replace(/\.zip$/i, "") || "Программа" }),
        });
        target = created.id;
        setProgramId(target);
        saveSession(target, "");
      }
      const data = new FormData();
      data.append("file", selected[0]);
      const accepted = await api<ImportJob>(`/api/v1/programs/${target}/imports`, {
        method: "POST", body: data, headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      setJob(accepted);
      setJobId(accepted.id);
      setNotice("");
      saveSession(target, accepted.id);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось загрузить архив");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function retry() {
    if (!job || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      const next = await api<ImportJob>(`/api/v1/programs/${programId}/imports/${job.id}/retry`, {
        method: "POST",
      });
      setJob(next);
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось повторить обработку");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function choose(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) void addFiles(event.target.files);
    event.target.value = "";
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault(); setDragging(false);
    void addFiles(event.dataTransfer.files);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Система проверки документов</div>
        <div className="user-avatar" aria-hidden="true">АК</div>
      </header>
      <div className="workspace">
        <aside className="sidebar" aria-label="Разделы приложения">
          <nav className="side-nav">
            {["Программа", "Чат", "Выгрузка"].map(item => (
              <div key={item} className={`nav-item${item === "Программа" ? " is-active" : ""}`}
                aria-current={item === "Программа" ? "page" : undefined}>{item}</div>
            ))}
          </nav>
        </aside>
        <main className="main-content"><div className="content-column">
          <section className="page-heading">
            <div><h1>Программа</h1><p className="eyebrow">Пакет документов и состояние обработки</p></div>
            <span className="status-pill status-pill--muted">
              {busy ? "Отправка запроса" : job ? labels[job.status] : "Ожидает загрузки"}
            </span>
          </section>
          <section className="documents-section" aria-labelledby="documents-title">
            <div className="section-heading"><h2 id="documents-title">Документы программы</h2></div>
            <div className={`upload-card card${dragging ? " is-dragging" : ""}`}
              onDragEnter={event => { event.preventDefault(); setDragging(true); }}
              onDragOver={event => event.preventDefault()}
              onDragLeave={event => {
                if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
              }} onDrop={drop}>
              <div className="upload-copy"><strong>Перетащите сюда ZIP-архив</strong>
                <span>Документы DOCX/XLSX, до 50 файлов. Архив — до 50 МиБ.</span></div>
              <input ref={input} className="visually-hidden" type="file" accept=".zip,application/zip"
                onChange={choose} disabled={busy} aria-label="Выбрать ZIP-архив с документами программы" />
              <button className="primary-button" type="button" disabled={busy}
                onClick={() => input.current?.click()}>{busy ? "Отправляем…" : "Выбрать файлы"}</button>
            </div>
            {notice && <p className="upload-notice" role="alert">{notice}</p>}
          </section>
          {job && <section className="processing-card card" aria-labelledby="processing-title">
            <div className="processing-heading"><h2 id="processing-title">Обработка документов</h2>
              <span className="status-pill status-pill--muted">{labels[job.status]}</span></div>
            <p>Попытка {job.attempt} · {job.files.length} документов</p>
            <progress max={100} value={job.progress} aria-label="Прогресс обработки" />
            <span> {job.progress}%</span>
            <ul className="document-list">{job.files.map(file => (
              <li className="document-row" key={file.document_id}>
                <div><a className="document-name"
                  href={`/api/v1/programs/${programId}/documents/${file.document_id}/download`}>
                  {file.member_name}</a>
                  {file.error_message && <p role="status">{file.error_message}</p>}</div>
                <span className="status-pill status-pill--muted">{labels[file.status]}</span>
              </li>
            ))}</ul>
            {job.issues.length > 0 && <ul>{job.issues.map((issue, index) => (
              <li key={index}>{issue.message}</li>
            ))}</ul>}
            {["completed", "partial"].includes(job.status) && <p>
              <a href={`/api/v1/programs/${programId}/imports/${job.id}/result`} target="_blank"
                rel="noreferrer">Открыть сохранённый результат разбора</a>
              <br />Результат сохранён отдельно. Исторические версии программы не изменены.
            </p>}
            {["failed", "partial"].includes(job.status) && job.files.some(f => f.status === "failed") &&
              <button className="primary-button" type="button" disabled={busy} onClick={() => void retry()}>
                Повторить неудачные файлы</button>}
          </section>}
        </div></main>
      </div>
    </div>
  );
}

export default App;
