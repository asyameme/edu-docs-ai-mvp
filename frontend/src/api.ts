export type ImportFile = {
  document_id: string;
  member_name: string;
  sha256: string;
  status: "pending" | "processing" | "completed" | "failed";
  error_code: string | null;
  error_message: string | null;
};

export type ImportIssueSeverity = "error" | "warning" | "info";

export type ImportIssue = {
  category: string;
  severity: ImportIssueSeverity;
  message: string;
  document_id?: string;
};

export type ImportJob = {
  id: string;
  program_id: string;
  attempt: number;
  status: "pending" | "processing" | "completed" | "partial" | "failed";
  progress: number;
  files: ImportFile[];
  issues: ImportIssue[];
  automatic_review_id?: string | null;
};

export type ProgramRead = {
  signature: string | null;
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

export type DecisionBatchRequest = {
  expected_version_id: string;
  selections: Array<{ conflict_id: string; variant_id: string }>;
  signature: string;
};

export type ReviewJob = {
  id: string;
  review_id: string;
  purpose: "prepare" | "check";
  input_version_id: string;
  output_version_id: string | null;
  status: "pending" | "processing" | "completed" | "failed" | "conflict";
  progress: number;
  progress_stage: string | null;
  message: string | null;
  analysis_version_id: string | null;
  current_review_version_id: string | null;
  document_ids?: string[] | null;
  competency_code?: string | null;
};

type GraphBuildJobBase = {
  id: string;
  pending_version_id: string;
  message: string | null;
};

export type GraphBuildJob =
  | (GraphBuildJobBase & {
    status: "pending" | "processing";
    output_version_id: null;
  })
  | (GraphBuildJobBase & {
    status: "completed";
    output_version_id: string;
  })
  | (GraphBuildJobBase & {
    status: "failed" | "conflict";
    output_version_id: null;
  });

export type PrepareReviewRequest = {
  import_job_id: string;
  expected_version_id: string;
  rerun_same_import: boolean;
};

export type ReviewMutation =
  | {
    op: "set_shared_indicator_text";
    document_id: string;
    indicator_indices: number[];
    action: "keep" | "split";
  }
  | {
    op: "set_competency_links";
    document_id: string;
    collection: "disciplines" | "curriculum_items";
    record_index: number;
    expected_codes: string[];
    codes: string[];
  }
  | {
    op: "set_document_value";
    document_id: string;
    field_path: string;
    expected_old_value: unknown;
    value: unknown;
  }
  | {
    op: "set_attribute";
    target_kind: "entity" | "relation";
    target_id: string;
    field_path: string;
    value: unknown;
  }
  | {
    op: "add_entity";
    target_kind: "entity";
    target_id: string;
    entity: { source_ref_ids: string[] };
  }
  | {
    op: "remove_entity";
    target_kind: "entity";
    target_id: string;
  }
  | {
    op: "add_relation" | "update_relation";
    target_kind: "relation";
    target_id: string;
    relation: { source_ref_ids: string[] };
  }
  | {
    op: "remove_relation";
    target_kind: "relation";
    target_id: string;
  };

export type ReviewVariant = {
  id: string;
  label: string;
  changes: ReviewMutation[];
};

export type RpdAlignmentSummary = {
  outcome_id: string | null;
  outcome_text: string | null;
  classification: "supported" | "gap" | "uncertain" | "invalid" | null;
  reason: string | null;
  course_evidence?: Array<{ id?: string; topic?: string; activity?: string }> | null;
  assessment_evidence?: Array<{
    id?: string;
    indicator_code?: string;
    indicator_text?: string;
    assessment_tool?: string;
  }> | null;
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
  source_document_ids?: string[];
  requires_decision: boolean;
  variants: ReviewVariant[];
  rpd_alignment?: RpdAlignmentSummary | null;
  // Развернутое пояснение для человека: что означают числа и что проверять.
  explanation?: string | null;
  effective_status?: string;
  decision?: {
    variant_id: string;
    author: string | null;
    custom_value?: string;
    changes?: ReviewMutation[];
  } | null;
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
  review?: {
    graph_ready?: boolean;
    review_id?: string;
    import_job_id?: string;
    analysis_version_id?: string;
    lifecycle_policy?: "automatic_source_v2";
    decisions: Array<{ conflict_id: string; variant_id: string; author: string | null }>;
    confirmation: { author: string | null } | null;
  } | null;
};

export type ReviewDetails = {
  lifecycle?: {
    policy: "automatic_source_v2";
    saved: boolean;
    needs_projection?: boolean;
    is_source: boolean;
    analysis_status: "pending" | "processing" | "completed" | "failed" | "conflict";
    check_job_id: string | null;
    check_status: "not_started" | "pending" | "processing" | "completed" | "failed" | "conflict";
    can_generate: boolean;
    generation_reason: string | null;
    unresolved_relations: number;
  } | null;
  version: {
    id: string;
    number: number;
    snapshot: ReviewSnapshot;
  };
  conflicts_count: number;
  issues_count: number;
  issues: ReviewIssue[];
  confirmed: boolean;
  confirmation: { accepted_warning_ids: string[]; author: string | null } | null;
  check_job_id: string | null;
  fresh_issue_ids: string[];
  check_report: CheckReport | null;
  graph_build: GraphBuildJob | null;
  manual_check_targets?: {
    rpd: Array<{ document_id: string; name: string }>;
    competencies: Array<{ code: string; formulation: string }>;
  };
  prepare_report: PrepareReport | null;
};

export type ChatConfigRead = { available: boolean };

export type ChatSessionRead = {
  id: string;
  program_id: string;
  version_id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type ChatProposedChange = {
  target_kind: "entity" | "relation";
  target_id: string;
  field_path: string;
  expected_old_value: unknown;
  new_value: unknown;
  reason: string;
  source_ref_ids: string[];
};

export type ChatOutput =
  | { kind: "answer"; message: string; sources: Array<{ id: string }> }
  | { kind: "clarification"; message: string; questions: string[] }
  | {
    kind: "change_proposal";
    summary: string;
    changes: ChatProposedChange[];
    unresolved_questions: string[];
  };

export type ChatTurnHistoryRead = {
  id: string;
  run_id: string;
  status: "pending" | "completed" | "failed";
  user_text: string;
  result: ChatOutput | null;
  created_at: string;
  completed_at: string | null;
  proposal_reverted: boolean;
};

export type ChatSessionHistoryRead = ChatSessionRead & { turns: ChatTurnHistoryRead[] };

export type CompetencyCoverageSummary = {
  competency_code: string;
  status: "supported" | "gap" | "uncertain";
  reason: string | null;
};

export type RuleCoverage = {
  rule_code: string;
  rule_revision: string;
  status: "passed" | "failed" | "needs_review" | "skipped" | "not_applicable";
  reason: string | null;
  // Заполняется только R-09: краткое резюме по каждой компетенции.
  competencies?: CompetencyCoverageSummary[] | null;
};

export type PrepareReport = {
  coverage: RuleCoverage[];
  issues: ReviewIssue[];
  ruleset?: {
    config?: {
      rules?: Array<{ id: string; status: string }>;
    };
  };
};

export type CheckReport = {
  ruleset: { required_rules: string[] };
  coverage: RuleCoverage[];
  issues: ReviewIssue[];
};

export type CheckJob = ReviewJob & {
  content_hash: string;
  report: CheckReport | null;
};

export type GenerationJob = {
  id: string;
  program_id: string;
  version_id: string | null;
  version_number: number | null;
  status: "pending" | "processing" | "completed" | "failed";
  error: string | null;
  size_bytes: number | null;
  sha256: string | null;
  created_at: string;
  // Что генерация изменила в документах и что оставила для ручной правки.
  applied: string[];
  notes: string[];
};

export type PackageDocument = {
  document_id: string;
  member_name: string;
  doc_type: string;
};

export type PackageComposition = {
  ready: boolean;
  documents: PackageDocument[];
};

export type OptionalRuleCode = "R-09" | "R-11";

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
  optionalRuleCheckJobIds?: Partial<Record<OptionalRuleCode, string>>;
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
    const optionalRuleCheckJobIds: Partial<Record<OptionalRuleCode, string>> = {};
    if (value.optionalRuleCheckJobIds && typeof value.optionalRuleCheckJobIds === "object"
      && !Array.isArray(value.optionalRuleCheckJobIds)) {
      const savedChecks = value.optionalRuleCheckJobIds as Record<string, unknown>;
      for (const code of ["R-09", "R-11"] as const) {
        if (typeof savedChecks[code] === "string") optionalRuleCheckJobIds[code] = savedChecks[code];
      }
    }
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
      ...(Object.keys(optionalRuleCheckJobIds).length > 0 ? { optionalRuleCheckJobIds } : {}),
    };
  } catch { return null; }
}

export function saveSession(session: ImportSession) {
  try { localStorage.setItem("education-import", JSON.stringify(session)); }
  catch {
    // Загрузка работает и при запрете локального хранилища браузера.
  }
}
