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

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, typeof data?.detail === "string"
      ? data.detail : `Запрос отклонён (${response.status})`);
  }
  return data as T;
}

export function loadSession(): { programId: string; jobId: string } | null {
  try {
    const saved = JSON.parse(localStorage.getItem("education-import") || "null");
    return saved && typeof saved.programId === "string" && typeof saved.jobId === "string"
      ? saved : null;
  } catch { return null; }
}

export function saveSession(programId: string, jobId: string) {
  try { localStorage.setItem("education-import", JSON.stringify({ programId, jobId })); }
  catch {
    // Загрузка работает и при запрете локального хранилища браузера.
  }
}
