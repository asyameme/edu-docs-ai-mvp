import type { ImportJob } from "./api";

const labels = {
  pending: "Ожидает обработки", processing: "Обрабатывается", completed: "Обработано",
  partial: "Обработано частично", failed: "Ошибка обработки",
};

type Props = {
  programId: string;
  job: ImportJob;
  busy: boolean;
  onRetry: () => Promise<void>;
};

export function ImportProgress({ programId, job, busy, onRetry }: Props) {
  return (
    <div aria-label="Состояние серверной обработки">
      <p role="status">{labels[job.status]} · Попытка {job.attempt} · {job.files.length} документов</p>
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
        <button className="primary-button" type="button" disabled={busy} onClick={() => void onRetry()}>
          Повторить неудачные файлы</button>}
    </div>
  );
}
