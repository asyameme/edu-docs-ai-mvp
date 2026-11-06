export type ImportFile = {
  document_id: string;
  member_name: string;
  sha256: string;
  status: "pending" | "processing" | "completed" | "failed";
  error_code: string | null;
  error_message: string | null;
};

export type ImportJob = {
  id: string;
  program_id: string;
  attempt: number;
  status: "pending" | "processing" | "completed" | "partial" | "failed";
  progress: number;
  files: ImportFile[];
  issues: { category: string; severity: string; message: string }[];
};

export type ProgramRead = {
  id: string;
  name: string;
  current_version_id: string;
  created_at: string;
};

export type VersionRead = {
  id: string;
  program_id: string;
  number: number;
  parent_version_id: string | null;
  origin: "initial" | "save" | "restore";
  created_at: string;
  snapshot: ReviewSnapshot;
};

export type ReviewJob = {
  id: string;
  review_id: string;
  purpose: "prepare" | "check";
  input_version_id: string;
  output_version_id: string | null;
  status: "pending" | "processing" | "completed" | "failed" | "conflict";
  progress: number;
  message: string | null;
  analysis_version_id: string | null;
  current_review_version_id: string | null;
};

export type ReviewMutation = {
  op: string;
  target_kind: "entity" | "relation";
  target_id: string;
  field_path?: string | null;
  value?: unknown;
  entity?: { source_ref_ids?: string[] };
  relation?: { source_ref_ids?: string[] };
};

export type ReviewVariant = {
  id: string;
  label: string;
  changes: ReviewMutation[];
};

export type ReviewIssue = {
  id: string;
  rule_code: string;
  rule_revision: string;
  type: string;
  severity: "warning" | "blocking";
  status: "open" | "needs_review";
  message: string;
  source_ref_ids: string[];
  requires_decision: boolean;
  variants: ReviewVariant[];
  effective_status?: string;
  decision?: { variant_id: string } | null;
};

export type ReviewSnapshot = {
  schema_version: number;
  entities: Array<{
    id: string;
    kind: string;
    attributes: Record<string, unknown>;
    source_ref_ids: string[];
  }>;
  source_refs: Array<{
    id: string;
    document_id: string;
    section: string | null;
    locator: string;
  }>;
};

export type ReviewDetails = {
  version: {
    id: string;
    number: number;
    snapshot: ReviewSnapshot;
  };
  conflicts_count: number;
  issues_count: number;
  issues: ReviewIssue[];
  confirmed: boolean;
  confirmation: { accepted_warning_ids: string[] } | null;
  check_job_id: string | null;
  check_report: CheckReport | null;
};

export type CheckReport = {
  ruleset: { required_rules: string[] };
  coverage: Array<{ rule_code: string; status: string; reason: string | null }>;
  issues: ReviewIssue[];
};

export type CheckJob = ReviewJob & {
  content_hash: string;
  report: CheckReport | null;
};

export type ImportSession = {
  programId: string;
  jobId: string;
  versionId?: string;
  importVersionId?: string;
  analysisVersionId?: string;
  reviewId?: string;
  prepareKey?: string;
  checkJobId?: string;
  checkVersionId?: string;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    const validationMessage = Array.isArray(detail)
      ? detail.map((item: { loc?: unknown[]; msg?: unknown }) => {
        const location = Array.isArray(item.loc) ? item.loc.filter((part) => typeof part === "string").join(" → ") : "";
        return typeof item.msg === "string" ? `${location ? `${location}: ` : ""}${item.msg}` : "";
      }).filter(Boolean).join("; ")
      : "";
    throw new ApiError(response.status, typeof detail === "string"
      ? detail : validationMessage || `Запрос отклонён (${response.status})`);
  }
  return data as T;
}

export function loadSession(): ImportSession | null {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem("education-import") || "null");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return null;
    const value = saved as Record<string, unknown>;
    if (typeof value.programId !== "string" || typeof value.jobId !== "string") return null;
    return {
      programId: value.programId,
      jobId: value.jobId,
      ...(typeof value.versionId === "string" ? { versionId: value.versionId } : {}),
      ...(typeof value.importVersionId === "string" ? { importVersionId: value.importVersionId } : {}),
      ...(typeof value.analysisVersionId === "string" ? { analysisVersionId: value.analysisVersionId } : {}),
      ...(typeof value.reviewId === "string" ? { reviewId: value.reviewId } : {}),
      ...(typeof value.prepareKey === "string" ? { prepareKey: value.prepareKey } : {}),
      ...(typeof value.checkJobId === "string" ? { checkJobId: value.checkJobId } : {}),
      ...(typeof value.checkVersionId === "string" ? { checkVersionId: value.checkVersionId } : {}),
    };
  } catch { return null; }
}

export function saveSession(session: ImportSession) {
  try { localStorage.setItem("education-import", JSON.stringify(session)); }
  catch {
    // Загрузка работает и при запрете локального хранилища браузера.
  }
}
