import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  loadSession,
  saveSession,
  type CheckJob,
  type ImportJob,
  type ImportSession,
  type OptionalRuleCode,
  type PrepareReviewRequest,
  type ProgramRead,
  type ReviewDetails,
  type ReviewJob,
  type VersionRead,
} from "./api";
import { signatureForProgram, type ProgramSignature } from "./signature";

const emptySession: ImportSession = { programId: "", jobId: "" };

function operationHeaders(idempotencyKey: string = crypto.randomUUID()): HeadersInit {
  return {
    "Content-Type": "application/json",
    "Idempotency-Key": idempotencyKey,
  };
}

export function useImportJob() {
  const initial = useRef(loadSession() ?? emptySession);
  const sessionRef = useRef(initial.current);
  const initialSessionKey = `${initial.current.programId}:${initial.current.jobId}`;
  const [recoveredReviewFor, setRecoveredReviewFor] = useState<string | null>(
    initial.current.reviewId ? initialSessionKey : null,
  );
  const prepareRequestRef = useRef("");
  const busyRef = useRef(false);
  const reviewBusyRef = useRef(false);
  const signatureRef = useRef<ProgramSignature | null>(null);
  const signatureSavingRef = useRef(false);
  const [signatureSaving, setSignatureSaving] = useState(false);
  const [signature, setSignature] = useState<ProgramSignature | null>(null);
  const [session, setSession] = useState(initial.current);
  const [program, setProgram] = useState<ProgramRead | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [reviewJob, setReviewJob] = useState<ReviewJob | null>(null);
  const [reviewDetails, setReviewDetails] = useState<ReviewDetails | null>(null);
  const reviewDetailsLoadsRef = useRef(new Map<string, Promise<ReviewDetails>>());
  const [checkJob, setCheckJob] = useState<CheckJob | null>(null);
  const [optionalRuleChecks, setOptionalRuleChecks] = useState<Partial<Record<OptionalRuleCode, CheckJob>>>({});
  const [busy, setBusy] = useState(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [prepareAttempt, setPrepareAttempt] = useState(0);
  const [rerunSameImport, setRerunSameImport] = useState(false);

  const updateSession = useCallback((patch: Partial<ImportSession>) => {
    const current = sessionRef.current;
    const next = { ...current, ...patch };
    if (patch.versionId && patch.versionId !== current.versionId) {
      delete next.optionalRuleCheckJobIds;
      setOptionalRuleChecks({});
    }
    sessionRef.current = next;
    setSession(next);
    saveSession(next);
  }, []);

  const openProgramState = useCallback((next: ProgramRead) => {
    const restored = next.signature ? { programId: next.id, value: next.signature } : null;
    signatureRef.current = restored;
    setSignature(restored);
    setProgram(next);
  }, []);

  const saveSignature = useCallback(async (value: string) => {
    if (signatureSavingRef.current) return false;
    const programId = sessionRef.current.programId;
    const normalized = value.trim();
    if (!program || program.id !== programId) return false;
    if (!normalized || Array.from(normalized).length > 120) {
      setNotice("Введите имя для подписи длиной от 1 до 120 символов.");
      return false;
    }
    signatureSavingRef.current = true;
    setSignatureSaving(true);
    try {
      const saved = await api<ProgramRead>(`/api/v1/programs/${programId}/signature`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: normalized }),
      });
      if (sessionRef.current.programId !== programId) return false;
      const next = saved.signature ? { programId, value: saved.signature } : null;
      signatureRef.current = next;
      setSignature(next);
      setProgram((current) => current?.id === programId
        ? { ...current, signature: saved.signature } : current);
      setNotice("");
      return true;
    } catch (error) {
      if (sessionRef.current.programId === programId) {
        setNotice(error instanceof Error ? error.message : "Не удалось сохранить подпись");
      }
      return false;
    } finally {
      signatureSavingRef.current = false;
      setSignatureSaving(false);
    }
  }, [program]);

  const loadReviewDetails = useCallback((versionId: string, programId = sessionRef.current.programId) => {
    const loadKey = `${programId}:${versionId}`;
    const existingLoad = reviewDetailsLoadsRef.current.get(loadKey);
    if (existingLoad) return existingLoad;

    const load = (async () => {
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
      if (sessionRef.current.programId === programId && sessionRef.current.versionId === versionId) {
        setReviewDetails(complete);
      }
      return complete;
    })();

    reviewDetailsLoadsRef.current.set(loadKey, load);
    void load.then(
      () => {
        if (reviewDetailsLoadsRef.current.get(loadKey) === load) reviewDetailsLoadsRef.current.delete(loadKey);
      },
      () => {
        if (reviewDetailsLoadsRef.current.get(loadKey) === load) reviewDetailsLoadsRef.current.delete(loadKey);
      },
    );
    return load;
  }, []);

  useEffect(() => {
    signatureRef.current = null;
    setSignature(null);
    setProgram(null);
    let active = true;
    const restoreSingleProgram = async () => {
      const programs = await api<ProgramRead[]>("/api/v1/programs?limit=2&offset=0");
      if (!active || programs.length !== 1) return;
      const restoredProgram = programs[0];
      try {
        const details = await api<ReviewDetails>(
          `/api/v1/programs/${restoredProgram.id}/versions/${restoredProgram.current_version_id}/review?limit=1&offset=0`,
        );
        const savedReview = details.version.snapshot.review;
        if (!savedReview?.review_id || !savedReview.import_job_id) return;
        const [restoredJob, restoredReviewJob] = await Promise.all([
          api<ImportJob>(`/api/v1/programs/${restoredProgram.id}/imports/${savedReview.import_job_id}`),
          api<ReviewJob>(`/api/v1/programs/${restoredProgram.id}/reviews/${savedReview.review_id}`),
        ]);
        if (!active) return;
        const restoredSession: ImportSession = {
          programId: restoredProgram.id,
          jobId: restoredJob.id,
          versionId: restoredProgram.current_version_id,
          importVersionId: restoredProgram.current_version_id,
          reviewId: savedReview.review_id,
          analysisVersionId: savedReview.analysis_version_id ?? undefined,
          ...(details.check_job_id ? {
            checkJobId: details.check_job_id,
            checkVersionId: restoredProgram.current_version_id,
          } : {}),
        };
        sessionRef.current = restoredSession;
        setSession(restoredSession);
        saveSession(restoredSession);
        openProgramState(restoredProgram);
        setJob(restoredJob);
        setReviewJob(restoredReviewJob);
        setCheckJob(null);
        setReviewDetails(null);
        setRecoveredReviewFor(`${restoredProgram.id}:${restoredJob.id}`);
        setNotice("");
        await loadReviewDetails(restoredProgram.current_version_id, restoredProgram.id);
      } catch (error) {
        if (active && !(error instanceof ApiError && error.status === 404)) {
          setNotice(error instanceof Error ? error.message : "Не удалось восстановить программу");
        }
      }
    };
    const openProgram = async () => {
      if (!session.programId) {
        await restoreSingleProgram();
        return;
      }
      try {
        const next = await api<ProgramRead>(`/api/v1/programs/${session.programId}`);
        if (!active) return;
        openProgramState(next);
        if (!sessionRef.current.versionId && !sessionRef.current.reviewId) {
          updateSession({ versionId: next.current_version_id });
        }
      } catch (error) {
        if (!active) return;
        if (error instanceof ApiError && error.status === 404) {
          await restoreSingleProgram();
          return;
        }
        setNotice(error instanceof Error ? error.message : "Не удалось открыть программу");
      }
    };
    void openProgram();
    return () => { active = false; };
  }, [session.programId, updateSession, loadReviewDetails, openProgramState]);

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

  useEffect(() => {
    const sessionKey = `${session.programId}:${session.jobId}`;
    if (
      !session.programId || !session.jobId || session.reviewId || !program || !job ||
      !["completed", "partial"].includes(job.status) || recoveredReviewFor === sessionKey
    ) return;

    let active = true;
    const recover = async () => {
      try {
        const details = await api<ReviewDetails>(
          `/api/v1/programs/${session.programId}/versions/${program.current_version_id}/review?limit=1&offset=0`,
        );
        const savedReview = details.version.snapshot.review;
        if (savedReview?.import_job_id === session.jobId && savedReview.review_id) {
          updateSession({
            versionId: program.current_version_id,
            reviewId: savedReview.review_id,
            analysisVersionId: savedReview.analysis_version_id,
            prepareKey: undefined,
          });
          setNotice("");
        }
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404) && active) {
          setNotice(error instanceof Error ? error.message : "Не удалось восстановить результаты сверки");
        }
      } finally {
        if (active) setRecoveredReviewFor(sessionKey);
      }
    };
    void recover();
    return () => { active = false; };
  }, [session.programId, session.jobId, session.reviewId, program?.current_version_id,
    job?.status, recoveredReviewFor, updateSession]);

  const canPrepareReview = Boolean(
    job && session.programId && session.versionId &&
    ["completed", "partial"].includes(job.status) && job.files.length > 0 &&
    job.files.every((file) => file.status === "completed") &&
    job.issues.every((issue) => issue.severity !== "error"),
  );

  useEffect(() => {
    const sessionKey = `${session.programId}:${session.jobId}`;
    if (!canPrepareReview || session.reviewId || recoveredReviewFor !== sessionKey || prepareRequestRef.current) return;
    if (!session.prepareKey) {
      updateSession({ prepareKey: crypto.randomUUID() });
      return;
    }
    const requestKey = session.prepareKey;
    prepareRequestRef.current = requestKey;
    setReviewBusy(true);
    setNotice("Импорт завершён. Готовим сверку документов…");
    const expectedVersionId = session.importVersionId ?? session.versionId;
    if (!expectedVersionId) {
      prepareRequestRef.current = "";
      setReviewBusy(false);
      setNotice("Не удалось определить версию импорта для сверки.");
      return;
    }
    const request: PrepareReviewRequest = {
      import_job_id: session.jobId,
      expected_version_id: expectedVersionId,
      rerun_same_import: rerunSameImport,
    };
    void api<ReviewJob>(`/api/v1/programs/${session.programId}/reviews`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": requestKey },
      body: JSON.stringify(request),
    }).then((next) => {
      setReviewJob(next);
      updateSession({ reviewId: next.review_id || next.id, prepareKey: undefined });
      setNotice("");
      setRerunSameImport(false);
    }).catch((error: unknown) => {
      setNotice(error instanceof Error ? error.message : "Не удалось подготовить сверку");
    }).finally(() => {
      prepareRequestRef.current = "";
      setReviewBusy(false);
    });
    return () => { prepareRequestRef.current = ""; };
  }, [canPrepareReview, session.programId, session.jobId, session.versionId, session.reviewId,
    session.prepareKey, prepareAttempt, recoveredReviewFor, rerunSameImport, updateSession]);

  useEffect(() => {
    if (!session.programId || !session.reviewId) return;
    let active = true;
    let stream: EventSource | undefined;
    let retryTimer: ReturnType<typeof setTimeout>;
    const recoverCurrentPrepare = async (failed: ReviewJob) => {
      const current = sessionRef.current;
      if (!current.programId || !current.jobId) return;
      setNotice("Сверка устарела. Восстанавливаем актуальный результат…");
      try {
        const latest = await api<ProgramRead>(`/api/v1/programs/${current.programId}`);
        const details = await api<ReviewDetails>(
          `/api/v1/programs/${current.programId}/versions/${latest.current_version_id}/review?limit=1&offset=0`,
        );
        const savedReview = details.version.snapshot.review;
        if (
          !savedReview?.review_id ||
          savedReview.import_job_id !== current.jobId ||
          savedReview.review_id === failed.review_id
        ) {
          setNotice(failed.message || "Не удалось подготовить сверку");
          return;
        }
        const recovered = await api<ReviewJob>(
          `/api/v1/programs/${current.programId}/reviews/${savedReview.review_id}`,
        );
        if (!active) return;
        if (recovered.status !== "completed" || !recovered.current_review_version_id) {
          setNotice(failed.message || "Не удалось подготовить сверку");
          return;
        }
        setReviewDetails(null);
        setCheckJob(null);
        updateSession({
          versionId: recovered.current_review_version_id,
          reviewId: recovered.review_id,
          analysisVersionId: savedReview.analysis_version_id ?? recovered.analysis_version_id ?? undefined,
        });
        setReviewJob(recovered);
        setNotice("");
      } catch (error) {
        if (!active) return;
        setNotice(error instanceof Error ? error.message : "Не удалось восстановить актуальную сверку");
      }
    };
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
      } else if (next.status === "conflict" && next.purpose === "prepare") {
        void recoverCurrentPrepare(next);
      } else if (next.status === "failed" || next.status === "conflict") {
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

  useEffect(() => {
    if (!session.programId || !session.reviewId || !session.optionalRuleCheckJobIds) return;
    let active = true;
    const timers = new Map<OptionalRuleCode, ReturnType<typeof setTimeout>>();
    const poll = async (ruleCode: OptionalRuleCode, jobId: string) => {
      try {
        const next = await api<CheckJob>(
          `/api/v1/programs/${session.programId}/reviews/${session.reviewId}/checks/${jobId}`,
        );
        if (!active || next.input_version_id !== session.versionId) return;
        setOptionalRuleChecks((current) => ({ ...current, [ruleCode]: next }));
        if (["pending", "processing"].includes(next.status)) {
          timers.set(ruleCode, setTimeout(() => void poll(ruleCode, jobId), 1200));
        }
      } catch (error) {
        if (!active) return;
        setNotice(error instanceof Error ? error.message : `Не удалось получить результат ${ruleCode}`);
        timers.set(ruleCode, setTimeout(() => void poll(ruleCode, jobId), 3000));
      }
    };
    for (const ruleCode of ["R-09", "R-11"] as const) {
      const jobId = session.optionalRuleCheckJobIds[ruleCode];
      if (jobId) void poll(ruleCode, jobId);
    }
    return () => {
      active = false;
      for (const timer of timers.values()) clearTimeout(timer);
    };
  }, [session.programId, session.reviewId, session.versionId, session.optionalRuleCheckJobIds]);

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
      openProgramState(programRead);
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
      setOptionalRuleChecks({});
      setRerunSameImport(false);

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
    const signature = signatureForProgram(signatureRef.current, current.programId);
    if (!signature) {
      setNotice("Укажите подпись для этой программы кнопкой над программой.");
      return;
    }
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const version = await api<VersionRead>(`/api/v1/programs/${current.programId}/reviews/${current.reviewId}/decisions`, {
        method: "POST",
        headers: operationHeaders(),
        body: JSON.stringify({ expected_version_id: current.versionId, conflict_id: conflictId, variant_id: variantId, signature }),
      });
      updateSession({ versionId: version.id, checkJobId: undefined, checkVersionId: undefined });
      setCheckJob(null);
      setNotice("");
      try {
        await loadReviewDetails(version.id, current.programId);
      } catch {
        setNotice("Выбор сохранён, обновляем список расхождений…");
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось сохранить выбранное значение");
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function acceptChatProposal(sessionId: string, turnId: string, expectedVersionId: string) {
    const current = sessionRef.current;
    if (reviewBusyRef.current || !current.programId || !current.versionId || !current.reviewId) return false;
    if (current.versionId !== expectedVersionId) {
      setNotice("Версия программы изменилась. Обновите предложение в чате перед применением.");
      return false;
    }
    const signature = signatureForProgram(signatureRef.current, current.programId);
    if (!signature) {
      setNotice("Укажите подпись для этой программы кнопкой над программой.");
      return false;
    }
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const accepted = await api<{ version: VersionRead; check_job_id: string }>(
        `/api/v1/programs/${current.programId}/chat/sessions/${sessionId}/runs/${turnId}/accept`,
        {
          method: "POST",
          headers: operationHeaders(),
          body: JSON.stringify({ expected_version_id: expectedVersionId, signature }),
        },
      );
      updateSession({
        versionId: accepted.version.id,
        checkJobId: accepted.check_job_id,
        checkVersionId: accepted.version.id,
      });
      setCheckJob(null);
      setNotice("");
      await loadReviewDetails(accepted.version.id, current.programId);
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось применить предложение агента");
      return false;
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  const runCheck = useCallback(async (idempotencyKey?: string) => {
    const current = sessionRef.current;
    if (reviewBusyRef.current || !current.programId || !current.reviewId || !current.versionId) return;
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const next = await api<ReviewJob>(`/api/v1/programs/${current.programId}/reviews/${current.reviewId}/checks`, {
        method: "POST",
        headers: operationHeaders(idempotencyKey),
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
  }, [updateSession]);

  async function runOptionalRuleCheck(ruleCode: OptionalRuleCode) {
    const current = sessionRef.current;
    const currentDetails = reviewDetails;
    const anotherCheckRunning = Object.values(optionalRuleChecks).some(
      (check) => check && ["pending", "processing"].includes(check.status),
    );
    const standardCheckRunning = Boolean(
      checkJob && ["pending", "processing"].includes(checkJob.status),
    );
    if (
      reviewBusyRef.current || anotherCheckRunning || standardCheckRunning ||
      !current.programId || !current.reviewId || !current.versionId ||
      !currentDetails || currentDetails.version.id !== current.versionId ||
      currentDetails.version.snapshot.review?.graph_ready !== true || currentDetails.confirmed
    ) return;
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const next = await api<ReviewJob>(
        `/api/v1/programs/${current.programId}/reviews/${current.reviewId}/checks`,
        {
          method: "POST",
          headers: operationHeaders(),
          body: JSON.stringify({ version_id: current.versionId, rule_codes: [ruleCode] }),
        },
      );
      const optionalRuleCheckJobIds = {
        ...(current.optionalRuleCheckJobIds ?? {}),
        [ruleCode]: next.id,
      };
      setOptionalRuleChecks((checks) => ({
        ...checks,
        [ruleCode]: { ...next, content_hash: "", report: null },
      }));
      updateSession({ optionalRuleCheckJobIds });
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : `Не удалось запустить ${ruleCode}`);
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function confirm(acceptedWarningIds: string[]) {
    const current = sessionRef.current;
    if (reviewBusyRef.current || !current.programId || !current.reviewId || !current.versionId || !checkJob || checkJob.status !== "completed") return;
    const signature = signatureForProgram(signatureRef.current, current.programId);
    if (!signature) {
      setNotice("Укажите подпись для этой программы кнопкой над программой.");
      return;
    }
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
          signature,
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
    setRerunSameImport(true);
    if (current.reviewId || (reviewJob && ["failed", "conflict", "completed"].includes(reviewJob.status))) {
      try {
        const latest = await api<ProgramRead>(`/api/v1/programs/${current.programId}`);
        openProgramState(latest);
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
        setRerunSameImport(false);
        return;
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
    signature: signatureForProgram(signature, session.programId),
    signatureSaving,
    saveSignature,
    job,
    reviewJob,
    reviewDetails,
    checkJob,
    optionalRuleChecks,
    busy,
    reviewBusy,
    notice,
    canPrepareReview,
    clearNotice: () => setNotice(""),
    upload,
    retry,
    retryPrepare,
    decide,
    acceptChatProposal,
    runCheck,
    runOptionalRuleCheck,
    confirm,
  };
}
