import type { ImportJob } from "./api";
import { DiagnosticList } from "./DiagnosticList";
import { groupDiagnostics, importDiagnostics } from "./diagnostics";

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
  const issues = importDiagnostics(job);
  const groups = groupDiagnostics(issues);
  const issueCounts = groups.reduce(
    (counts, issue) => {
      counts[issue.severity] += 1;
      return counts;
    },
    { error: 0, warning: 0, info: 0 },
  );
  const issueSummary = [
    issueCounts.error > 0 ? `Групп ошибок: ${issueCounts.error}` : "",
    issueCounts.warning > 0 ? `Групп предупреждений: ${issueCounts.warning}` : "",
    issueCounts.info > 0 ? `Информационных групп: ${issueCounts.info}` : "",
  ].filter(Boolean).join(" · ");

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
      {issues.length > 0 && <details className="issues-details">
        <summary>Замечания парсера · {issueSummary}</summary>
        <DiagnosticList items={issues} />
      </details>}
      {["failed", "partial"].includes(job.status) && job.files.some(f => f.status === "failed") &&
        <button className="primary-button" type="button" disabled={busy} onClick={() => void onRetry()}>
          Повторить неудачные файлы</button>}
    </div>
  );
}
