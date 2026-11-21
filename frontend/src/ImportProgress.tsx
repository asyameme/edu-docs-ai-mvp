import type { ImportJob } from "./api";
import { groupImportIssues, importIssueSeverityLabels } from "./importIssues";

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
  const issues = groupImportIssues(job);
  const issueCounts = issues.reduce(
    (counts, issue) => {
      counts[issue.severity] += issue.count;
      return counts;
    },
    { error: 0, warning: 0, info: 0 },
  );
  const issueSummary = [
    issueCounts.error > 0 ? `Ошибки: ${issueCounts.error}` : "",
    issueCounts.warning > 0 ? `Предупреждения: ${issueCounts.warning}` : "",
    issueCounts.info > 0 ? `Информация: ${issueCounts.info}` : "",
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
        <ul className="issue-list">{issues.map((issue) => (
          <li className="issue-row" key={issue.id}>
            <span className={`severity-dot severity-dot--${issue.severity}`} aria-hidden="true" />
            <span className="issue-copy">
              <strong>{issue.message}{issue.count > 1 ? ` · ×${issue.count}` : ""}</strong>
              <span>{issue.documentName ?? "Разбор ZIP-пакета"} · {issue.category}</span>
            </span>
            <span className={`severity-label severity-label--${issue.severity}`}>
              {importIssueSeverityLabels[issue.severity]}
            </span>
          </li>
        ))}</ul>
      </details>}
      {["completed", "partial"].includes(job.status) && <p>
        <a href={`/api/v1/programs/${programId}/imports/${job.id}/result`} target="_blank"
          rel="noreferrer">Открыть результаты парсинга</a>
        <br />JSON документов сохранены отдельно; граф программы будет собран после согласования.
      </p>}
      {["failed", "partial"].includes(job.status) && job.files.some(f => f.status === "failed") &&
        <button className="primary-button" type="button" disabled={busy} onClick={() => void onRetry()}>
          Повторить неудачные файлы</button>}
    </div>
  );
}
