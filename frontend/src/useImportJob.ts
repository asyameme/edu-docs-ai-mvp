import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  loadSession,
  saveSession,
  type CheckJob,
  type ImportJob,
  type ImportSession,
  type ProgramRead,
  type ReviewDetails,
  type ReviewJob,
  type VersionRead,
} from "./api";

const emptySession: ImportSession = { programId: "", jobId: "" };

function operationHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json",
    "Idempotency-Key": crypto.randomUUID(),
  };
}

export function useImportJob() {
  const initial = useRef(loadSession() ?? emptySession);
  const sessionRef = useRef(initial.current);
  const prepareRequestRef = useRef("");
  const busyRef = useRef(false);
  const reviewBusyRef = useRef(false);
  const [session, setSession] = useState(initial.current);
  const [program, setProgram] = useState<ProgramRead | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [reviewJob, setReviewJob] = useState<ReviewJob | null>(null);
  const [reviewDetails, setReviewDetails] = useState<ReviewDetails | null>(null);
  const [checkJob, setCheckJob] = useState<CheckJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [prepareAttempt, setPrepareAttempt] = useState(0);

  const updateSession = useCallback((patch: Partial<ImportSession>) => {
    const next = { ...sessionRef.current, ...patch };
    sessionRef.current = next;
    setSession(next);
    saveSession(next);
  }, []);

  const loadReviewDetails = useCallback(async (versionId: string, programId = sessionRef.current.programId) => {
    const readPage = (id: string, offset: number) => api<ReviewDetails>(
      `/api/v1/programs/${programId}/versions/${id}/review?limit=100&offset=${offset}`,
    );
    const details = await readPage(versionId, 0);
    const issues = [...details.issues];
    for (let offset = issues.length; offset < details.issues_count; offset += 100) {
      const page = await readPage(versionId, offset);
      if (page.issues.length === 0) break;
      issues.push(...page.issues);
    }
    const current = sessionRef.current;
    const sourceVersionIds = [...new Set([current.analysisVersionId, current.checkVersionId]
      .filter((id): id is string => Boolean(id) && id !== versionId))];
    const sourceDetails = await Promise.all(sourceVersionIds.map(async (id) => {
      try { return await readPage(id, 0); }
      catch { return null; }
    }));
    const refs = new Map(details.version.snapshot.source_refs.map((source) => [source.id, source]));
    sourceDetails.forEach((source) => source?.version.snapshot.source_refs.forEach((item) => refs.set(item.id, item)));
    const complete = {
      ...details,
      issues,
      version: {
        ...details.version,
        snapshot: { ...details.version.snapshot, source_refs: [...refs.values()] },
      },
    };
    setReviewDetails(complete);
    return complete;
  }, []);

  useEffect(() => {
    if (!session.programId) return;
    let active = true;
    void api<ProgramRead>(`/api/v1/programs/${session.programId}`)
      .then((next) => {
        if (!active) return;
        setProgram(next);
        if (!sessionRef.current.versionId && !sessionRef.current.reviewId) {
          updateSession({ versionId: next.current_version_id });
        }
      })
      .catch((error: unknown) => {
        if (active && !(error instanceof ApiError && error.status === 404)) {
          setNotice(error instanceof Error ? error.message : "Не удалось открыть программу");
        }
      });
    return () => { active = false; };
  }, [session.programId, updateSession]);

  useEffect(() => {
    if (!session.programId || !session.jobId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let stream: EventSource | undefined;
    const connect = async () => {
      try {
        const next = await api<ImportJob>(`/api/v1/programs/${session.programId}/imports/${session.jobId}`);
        if (!active) return;
        setJob(next);
        if (["pending", "processing"].includes(next.status)) {
          stream = new EventSource(`/api/v1/programs/${session.programId}/imports/${session.jobId}/events`);
          stream.addEventListener("status", (event) => {
            if (!active) return;
            try {
              const updated = JSON.parse((event as MessageEvent<string>).data) as ImportJob;
              setJob(updated);
              if (!["pending", "processing"].includes(updated.status)) stream?.close();
            } catch {
              setNotice("Сервер прислал некорректное состояние обработки.");
            }
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
  }, [session.programId, session.jobId, job?.attempt]);

  const canPrepareReview = Boolean(
    job && session.programId && session.versionId &&
    ["completed", "partial"].includes(job.status) && job.files.length > 0 &&
    job.files.every((file) => file.status === "completed") &&
    job.issues.every((issue) => issue.severity !== "error"),
  );

  useEffect(() => {
    if (!canPrepareReview || session.reviewId || prepareRequestRef.current) return;
    if (!session.prepareKey) {
      updateSession({ prepareKey: crypto.randomUUID() });
      return;
    }
    const requestKey = session.prepareKey;
    prepareRequestRef.current = requestKey;
    setReviewBusy(true);
    setNotice("Импорт завершён. Готовим сверку документов…");
    void api<ReviewJob>(`/api/v1/programs/${session.programId}/reviews`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": requestKey },
      body: JSON.stringify({
        import_job_id: session.jobId,
        expected_version_id: session.importVersionId ?? session.versionId,
      }),
    }).then((next) => {
      setReviewJob(next);
      updateSession({ reviewId: next.review_id || next.id, prepareKey: undefined });
      setNotice("");
    }).catch((error: unknown) => {
      setNotice(error instanceof Error ? error.message : "Не удалось подготовить сверку");
    }).finally(() => {
      prepareRequestRef.current = "";
      setReviewBusy(false);
    });
    return () => { prepareRequestRef.current = ""; };
  }, [canPrepareReview, session.programId, session.jobId, session.versionId, session.reviewId,
    session.prepareKey, prepareAttempt, updateSession]);

  useEffect(() => {
    if (!session.programId || !session.reviewId) return;
    let active = true;
    let stream: EventSource | undefined;
    let retryTimer: ReturnType<typeof setTimeout>;
    const receive = (next: ReviewJob) => {
      if (!active) return;
      setReviewJob(next);
      if (next.status === "completed") {
        const currentVersionId = next.current_review_version_id ?? next.analysis_version_id ?? next.output_version_id;
        const analysisVersionId = next.analysis_version_id ?? next.output_version_id ?? undefined;
        if (currentVersionId && currentVersionId !== sessionRef.current.versionId) {
          updateSession({ versionId: currentVersionId, analysisVersionId });
        } else if (analysisVersionId && analysisVersionId !== sessionRef.current.analysisVersionId) {
          updateSession({ analysisVersionId });
        }
        setNotice("");
      } else if (["failed", "conflict"].includes(next.status)) {
        setNotice(next.message || "Не удалось подготовить сверку");
      }
    };
    const connect = () => {
      void api<ReviewJob>(`/api/v1/programs/${session.programId}/reviews/${session.reviewId}`)
        .then((next) => {
          if (!active) return;
          receive(next);
          if (["pending", "processing"].includes(next.status)) {
            stream = new EventSource(`/api/v1/programs/${session.programId}/reviews/${session.reviewId}/events`);
            stream.addEventListener("status", (event) => {
              try {
                const state = JSON.parse((event as MessageEvent<string>).data) as ReviewJob;
                receive(state);
                if (!["pending", "processing"].includes(state.status)) stream?.close();
              } catch {
                setNotice("Сервер прислал некорректное состояние сверки.");
              }
            });
            stream.addEventListener("unavailable", () => {
              if (active) setNotice("Сверка временно недоступна. Повторяем подключение…");
            });
          }
        })
        .catch((error: unknown) => {
          if (!active) return;
          setNotice(error instanceof Error ? error.message : "Не удалось получить состояние сверки");
          if (!(error instanceof ApiError && error.status === 404)) retryTimer = setTimeout(connect, 5000);
        });
    };
    connect();
    return () => { active = false; clearTimeout(retryTimer); stream?.close(); };
  }, [session.programId, session.reviewId, updateSession]);

  useEffect(() => {
    const versionId = session.versionId;
    if (!session.programId || !session.reviewId || reviewJob?.status !== "completed" || !versionId) return;
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout>;
    const load = () => {
      void loadReviewDetails(versionId, session.programId)
        .then((details) => {
          if (active) setReviewDetails(details);
        })
        .catch((error: unknown) => {
          if (!active) return;
          setNotice(error instanceof Error ? error.message : "Не удалось загрузить результаты сверки");
          if (!(error instanceof ApiError && error.status === 404)) retryTimer = setTimeout(load, 5000);
        });
    };
    load();
    return () => { active = false; clearTimeout(retryTimer); };
  }, [session.programId, session.reviewId, session.versionId, reviewJob?.status, loadReviewDetails]);

  useEffect(() => {
    if (!session.programId || !session.reviewId || !session.checkJobId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api<CheckJob>(
          `/api/v1/programs/${session.programId}/reviews/${session.reviewId}/checks/${session.checkJobId}`,
        );
        if (!active) return;
        setCheckJob(next);
        if (["pending", "processing"].includes(next.status)) {
          timer = setTimeout(() => void poll(), 1200);
          return;
        }
        if (next.status === "completed") {
          setNotice("");
          if (sessionRef.current.versionId) {
            await loadReviewDetails(sessionRef.current.versionId, session.programId);
          }
        } else {
          setNotice(next.message || "Не удалось проверить версию");
        }
      } catch (error) {
        if (!active) return;
        setNotice(error instanceof Error ? error.message : "Не удалось получить состояние проверки");
        timer = setTimeout(() => void poll(), 3000);
      }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [session.programId, session.reviewId, session.checkJobId, loadReviewDetails]);

  async function upload(file: File): Promise<boolean> {
    if (busyRef.current) return false;
    if (!/\.zip$/i.test(file.name)) {
      setNotice("Выберите один ZIP-архив с документами DOCX/XLSX.");
      return false;
    }
    if (file.size > 500 * 1024 * 1024) {
      setNotice("Размер архива не должен превышать 500 МиБ.");
      return false;
    }
    busyRef.current = true;
    setBusy(true);
    setNotice("Отправляем архив на сервер…");
    try {
      let target = sessionRef.current.programId;
      let programRead: ProgramRead | null = null;
      if (target) {
        try {
          programRead = await api<ProgramRead>(`/api/v1/programs/${target}`);
        } catch (error) {
          if (error instanceof ApiError && error.status === 404) target = "";
          else throw error;
        }
      }
      if (!target) {
        programRead = await api<ProgramRead>("/api/v1/programs", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: file.name.replace(/\.zip$/i, "") || "Программа" }),
        });
        target = programRead.id;
      }
      if (!programRead) throw new Error("Не удалось создать программу");
      setProgram(programRead);
      const nextSession: ImportSession = {
        programId: target,
        jobId: "",
        versionId: programRead.current_version_id,
        importVersionId: programRead.current_version_id,
      };
      sessionRef.current = nextSession;
      setSession(nextSession);
      saveSession(nextSession);
      setJob(null);
      setReviewJob(null);
      setReviewDetails(null);
      setCheckJob(null);

      const data = new FormData();
      data.append("file", file);
      const accepted = await api<ImportJob>(`/api/v1/programs/${target}/imports`, {
        method: "POST", body: data, headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      setJob(accepted);
      updateSession({ jobId: accepted.id });
      setNotice("");
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось загрузить архив");
      return false;
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
      const next = await api<ImportJob>(`/api/v1/programs/${session.programId}/imports/${job.id}/retry`, {
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

  async function decide(conflictId: string, variantId: string) {
    const current = sessionRef.current;
    if (reviewBusyRef.current || !current.programId || !current.reviewId || !current.versionId) return;
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const version = await api<VersionRead>(`/api/v1/programs/${current.programId}/reviews/${current.reviewId}/decisions`, {
        method: "POST",
        headers: operationHeaders(),
        body: JSON.stringify({ expected_version_id: current.versionId, conflict_id: conflictId, variant_id: variantId }),
      });
      updateSession({ versionId: version.id, checkJobId: undefined, checkVersionId: undefined });
      setReviewDetails(null);
      setCheckJob(null);
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось сохранить выбранное значение");
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function runCheck() {
    const current = sessionRef.current;
    if (reviewBusyRef.current || !current.programId || !current.reviewId || !current.versionId) return;
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const next = await api<ReviewJob>(`/api/v1/programs/${current.programId}/reviews/${current.reviewId}/checks`, {
        method: "POST",
        headers: operationHeaders(),
        body: JSON.stringify({ version_id: current.versionId }),
      });
      setCheckJob({ ...next, content_hash: "", report: null });
      updateSession({ checkJobId: next.id, checkVersionId: current.versionId });
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось запустить проверку версии");
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function confirm(acceptedWarningIds: string[]) {
    const current = sessionRef.current;
    if (reviewBusyRef.current || !current.programId || !current.reviewId || !current.versionId || !checkJob || checkJob.status !== "completed") return;
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const version = await api<VersionRead>(`/api/v1/programs/${current.programId}/reviews/${current.reviewId}/confirm`, {
        method: "POST",
        headers: operationHeaders(),
        body: JSON.stringify({
          expected_version_id: current.versionId,
          check_job_id: checkJob.id,
          accepted_warning_ids: acceptedWarningIds,
        }),
      });
      updateSession({ versionId: version.id });
      setNotice("");
      await loadReviewDetails(version.id, current.programId);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось подтвердить версию");
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function retryPrepare() {
    const current = sessionRef.current;
    if (reviewJob && ["failed", "conflict"].includes(reviewJob.status)) {
      try {
        const latest = await api<ProgramRead>(`/api/v1/programs/${current.programId}`);
        setProgram(latest);
        setReviewJob(null);
        setReviewDetails(null);
        setCheckJob(null);
        updateSession({
          versionId: latest.current_version_id,
          importVersionId: latest.current_version_id,
          reviewId: undefined,
          analysisVersionId: undefined,
          checkJobId: undefined,
          checkVersionId: undefined,
          prepareKey: crypto.randomUUID(),
        });
      } catch (error) {
        setNotice(error instanceof Error ? error.message : "Не удалось повторно подготовить сверку");
      }
    } else if (!current.prepareKey) {
      updateSession({ prepareKey: crypto.randomUUID() });
    }
    setPrepareAttempt((attempt) => attempt + 1);
  }

  return {
    programId: session.programId,
    versionId: session.versionId ?? "",
    program,
    job,
    reviewJob,
    reviewDetails,
    checkJob,
    busy,
    reviewBusy,
    notice,
    canPrepareReview,
    clearNotice: () => setNotice(""),
    upload,
    retry,
    retryPrepare,
    decide,
    runCheck,
    confirm,
  };
}
