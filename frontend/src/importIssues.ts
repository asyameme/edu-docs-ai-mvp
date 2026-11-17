import type { ImportIssueSeverity, ImportJob } from "./api";

export const importIssueSeverityLabels: Record<ImportIssueSeverity, string> = {
  error: "Ошибка",
  warning: "Предупреждение",
  info: "Информация",
};

export type GroupedImportIssue = {
  id: string;
  category: string;
  severity: ImportIssueSeverity;
  message: string;
  documentName: string | null;
  count: number;
};

export function groupImportIssues(job: Pick<ImportJob, "files" | "issues">): GroupedImportIssue[] {
  const fileNames = new Map(job.files.map((file) => [file.document_id, file.member_name]));
  const groups = new Map<string, GroupedImportIssue>();

  for (const issue of job.issues) {
    const documentId = issue.document_id ?? "";
    const id = JSON.stringify([issue.category, issue.severity, documentId, issue.message]);
    const existing = groups.get(id);
    if (existing) {
      existing.count += 1;
      continue;
    }

    groups.set(id, {
      id,
      category: issue.category,
      severity: issue.severity,
      message: issue.message,
      documentName: fileNames.get(documentId) ?? null,
      count: 1,
    });
  }

  return [...groups.values()];
}
