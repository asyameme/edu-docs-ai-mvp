import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  loadSession,
  saveSession,
  type CheckJob,
  type DecisionBatchRequest,
  type GraphBuildJob,
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
import { levelOnlyVariantTargets } from "./levelVariants";
import { ruleDisplayLabel } from "./diagnostics";

const emptySession: ImportSession = { programId: "", jobId: "" };

type OptionalRuleLaunch = {
  ruleCode: OptionalRuleCode;
  versionId: string;
} & ({ status: "submitting" } | { status: "failed"; message: string });

type ChatProposalAcceptance =
  | { accepted: true; appliedVersionId: string }
  | { accepted: false; message: string };

type ChatProposalUndo =
  | { restored: true; versionId: string }
  | { restored: false; message: string };

function operationHeaders(idempotencyKey: string = crypto.randomUUID()): HeadersInit {
  return {
    "Content-Type": "application/json",
    "Idempotency-Key": idempotencyKey,
  };
}

export function useImportJob() {
  const initial = useRef(loadSession() ?? emptySession);
  const sessionRef = useRef(initial.current);
  // Всегда проверяем сохранённую сверку один раз: reviewId мог протухнуть,
  // если подготовку перезапустили в другой вкладке или снаружи.
  const [recoveredReviewFor, setRecoveredReviewFor] = useState<string | null>(null);
  const prepareRequestRef = useRef("");
  const busyRef = useRef(false);
  const reviewBusyRef = useRef(false);
  const signatureRef = useRef<ProgramSignature | null>(null);
  const signatureSavingRef = useRef(false);
  const [signatureSaving, setSignatureSaving] = useState(false);
  const [signature, setSignature] = useState<ProgramSignature | null>(null);
  const [session, setSession] = useState(initial.current);
  const [openingProgram, setOpeningProgram] = useState(true);
  const [program, setProgram] = useState<ProgramRead | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [reviewJob, setReviewJob] = useState<ReviewJob | null>(null);
  const [reviewDetails, setReviewDetails] = useState<ReviewDetails | null>(null);
  const [graphBuildJob, setGraphBuildJob] = useState<GraphBuildJob | null>(null);
  const graphBuildHandledRef = useRef("");
  const reviewDetailsLoadsRef = useRef(new Map<string, Promise<ReviewDetails>>());
  const [checkJob, setCheckJob] = useState<CheckJob | null>(null);
  const [optionalRuleChecks, setOptionalRuleChecks] = useState<Partial<Record<OptionalRuleCode, CheckJob>>>({});
  const [optionalRuleLaunch, setOptionalRuleLaunch] = useState<OptionalRuleLaunch | null>(null);
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

  // Программа из сохранённой сессии исчезла (например, БД пересоздали):
  // молча возвращаем страницу к начальному экрану загрузки.
  const resetStaleSession = useCallback(() => {
    sessionRef.current = emptySession;
    setSession(emptySession);
    saveSession(emptySession);
    setProgram(null);
    setJob(null);
    setReviewJob(null);
    setReviewDetails(null);
    setGraphBuildJob(null);
    signatureRef.current = null;
    setSignature(null);
    setCheckJob(null);
    setOptionalRuleChecks({});
    setRecoveredReviewFor(null);
    setNotice("");
  }, []);

  const applyProgramState = useCallback((latest: ProgramRead, analysisVersionId?: string | null) => {
    if (sessionRef.current.programId !== latest.id) return;
    openProgramState(latest);

    const current = sessionRef.current;
    const patch: Partial<ImportSession> = {};
    if (current.versionId !== latest.current_version_id) {
      patch.versionId = latest.current_version_id;
      patch.checkJobId = undefined;
      patch.checkVersionId = undefined;
      setCheckJob(null);
    }
    if (analysisVersionId && current.analysisVersionId !== analysisVersionId) {
      patch.analysisVersionId = analysisVersionId;
    }
    if (Object.keys(patch).length > 0) updateSession(patch);
  }, [updateSession, openProgramState]);

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
      const active = sessionRef.current;
      if (active.programId === programId && active.versionId === versionId) {
        setReviewDetails(complete);
        if (!complete.confirmed && complete.check_job_id && complete.check_report &&
          (!active.checkJobId || active.checkVersionId !== versionId)
        ) {
          updateSession({ checkJobId: complete.check_job_id, checkVersionId: versionId });
        }
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
  }, [updateSession]);

  useEffect(() => {
    const pendingBuild = reviewDetails?.graph_build;
    if (!pendingBuild || !session.programId) {
      setGraphBuildJob(null);
      return;
    }

    setGraphBuildJob(pendingBuild);
    let active = true;
    let timer: number | undefined;
    const reviewId = reviewDetails.version.snapshot.review?.review_id;
    const programId = session.programId;

    const applyBuiltVersion = async (build: GraphBuildJob) => {
      if (build.status !== "completed") return;
      const versionId = build.output_version_id;
      if (graphBuildHandledRef.current === build.id) return;
      graphBuildHandledRef.current = build.id;
      updateSession({ versionId, checkJobId: undefined, checkVersionId: undefined });
      setCheckJob(null);
      try {
        await loadReviewDetails(versionId, programId);
        if (active) setGraphBuildJob(null);
      } catch {
        if (active) setNotice("Граф программы собран. Обновляем данные версии…");
      }
    };

    if (pendingBuild.status === "completed") {
      void applyBuiltVersion(pendingBuild);
      return () => { active = false; };
    }
    if (pendingBuild.status === "failed" || pendingBuild.status === "conflict") {
      setNotice(pendingBuild.message || "Не удалось завершить сборку графа программы.");
      return () => { active = false; };
    }
    if (!reviewId) return () => { active = false; };

    const poll = async () => {
      try {
        const latest = await api<GraphBuildJob>(
          `/api/v1/programs/${programId}/reviews/${reviewId}/graph-build`,
        );
        if (!active) return;
        setGraphBuildJob(latest);
        if (latest.status === "completed") {
          await applyBuiltVersion(latest);
          return;
        }
        if (latest.status === "failed" || latest.status === "conflict") {
          setNotice(latest.message || "Не удалось завершить сборку графа программы.");
          try {
            const currentVersionId = sessionRef.current.versionId;
            if (currentVersionId) {
              const refreshed = await loadReviewDetails(currentVersionId, programId);
              if (active) setReviewDetails(refreshed);
            }
          } catch {
            // Сохраняем исходное предупреждение; список замечаний обновится при следующем чтении.
          }
          return;
        }
      } catch {
        // Временная ошибка чтения статуса не отменяет устойчивую задачу Temporal.
      }
      if (active) timer = window.setTimeout(() => void poll(), 1500);
    };

    timer = window.setTimeout(() => void poll(), 700);
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    reviewDetails?.graph_build?.id,
    reviewDetails?.graph_build?.status,
    reviewDetails?.version.snapshot.review?.review_id,
    session.programId,
    updateSession,
    loadReviewDetails,
  ]);

  useEffect(() => {
    setOpeningProgram(true);
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
        applyProgramState(next);
      } catch (error) {
        if (!active) return;
        if (error instanceof ApiError && error.status === 404) {
          await restoreSingleProgram();
          if (active && sessionRef.current.programId === session.programId) {
            // Восстановить нечего: программы больше нет — сбрасываем сессию,
            // чтобы страница вернулась к начальному экрану загрузки.
            resetStaleSession();
          }
          return;
        }
        setNotice(error instanceof Error ? error.message : "Не удалось открыть программу");
      }
    };
    void openProgram().finally(() => { if (active) setOpeningProgram(false); });
    return () => { active = false; };
  }, [session.programId, applyProgramState, loadReviewDetails, openProgramState, resetStaleSession]);

  useEffect(() => {
    if (!session.programId) return;
    let active = true;
    let refreshing = false;
    const refreshCurrentVersion = () => {
      if (!active || refreshing || document.visibilityState === "hidden") return;
      refreshing = true;
      void api<ProgramRead>(`/api/v1/programs/${session.programId}`)
        .then((latest) => {
          if (active) applyProgramState(latest);
        })
        .catch(() => {
          // Keep the visible results; the next focus/visibility event retries the refresh.
        })
        .finally(() => { refreshing = false; });
    };
    window.addEventListener("focus", refreshCurrentVersion);
    document.addEventListener("visibilitychange", refreshCurrentVersion);
    return () => {
      active = false;
      window.removeEventListener("focus", refreshCurrentVersion);
      document.removeEventListener("visibilitychange", refreshCurrentVersion);
    };
  }, [session.programId, applyProgramState]);

  useEffect(() => {
    let active = true;
    let refreshing = false;
    let refreshAgain = false;
    const refreshProgram = () => {
      if (refreshing) {
        refreshAgain = true;
        return;
      }
      refreshing = true;
      void api<ProgramRead>(`/api/v1/programs/${sessionRef.current.programId}`)
        .then((latest) => {
          if (active) applyProgramState(latest);
        })
        .catch(() => {
          // The tab keeps its current results and retries after the next update.
        })
        .finally(() => {
          refreshing = false;
          if (active && refreshAgain) {
            refreshAgain = false;
            refreshProgram();
          }
        });
    };
    const syncVersionFromAnotherTab = (event: StorageEvent) => {
      if (event.key !== "education-import" || !event.newValue) return;
      const external = loadSession();
      const current = sessionRef.current;
      if (
        !external || external.programId !== current.programId || external.jobId !== current.jobId ||
        external.reviewId !== current.reviewId || !external.versionId || external.versionId === current.versionId
      ) return;
      refreshProgram();
    };
    window.addEventListener("storage", syncVersionFromAnotherTab);
    return () => {
      active = false;
      window.removeEventListener("storage", syncVersionFromAnotherTab);
    };
  }, [applyProgramState]);

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
        if (error instanceof ApiError && error.status === 404) {
          // Задание импорта исчезло вместе с программой (БД пересоздали).
          resetStaleSession();
          return;
        }
        setNotice(error instanceof Error ? error.message : "Не удалось получить состояние задачи");
        timer = setTimeout(connect, 5000);
      }
    };
    void connect();
    return () => { active = false; clearTimeout(timer); stream?.close(); };
  }, [session.programId, session.jobId, job?.attempt, resetStaleSession]);

  useEffect(() => {
    const sessionKey = `${session.programId}:${session.jobId}`;
    if (
      !session.programId || !session.jobId || !program || !job ||
      !["completed", "partial"].includes(job.status) || recoveredReviewFor === sessionKey
    ) return;

    let active = true;
    const recover = async () => {
      try {
        const details = await api<ReviewDetails>(
          `/api/v1/programs/${session.programId}/versions/${program.current_version_id}/review?limit=1&offset=0`,
        );
        const savedReview = details.version.snapshot.review;
        // Сверку могли переподготовить в другой вкладке или снаружи: тогда
        // сохранённый reviewId протух, и решения и проверки уходят не в ту
        // подготовку с ошибкой «Версия не принадлежит этой подготовке».
        const staleReview = Boolean(
          session.reviewId && savedReview?.review_id && savedReview.review_id !== session.reviewId
        );
        if (
          (!session.reviewId || staleReview) &&
          savedReview?.import_job_id === session.jobId && savedReview.review_id
        ) {
          updateSession({
            versionId: program.current_version_id,
            reviewId: savedReview.review_id,
            analysisVersionId: savedReview.analysis_version_id,
            prepareKey: undefined,
            checkJobId: undefined,
            checkVersionId: undefined,
          });
          setCheckJob(null);
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
      if (!current.programId || !current.jobId || current.reviewId !== failed.review_id) return;
      setNotice("Сверка устарела. Восстанавливаем актуальный результат…");
      try {
        const latest = await api<ProgramRead>(`/api/v1/programs/${current.programId}`);
        const details = await api<ReviewDetails>(
          `/api/v1/programs/${current.programId}/versions/${latest.current_version_id}/review?limit=1&offset=0`,
        );
        const savedReview = details.version.snapshot.review;
        if (!active) return;
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
        setNotice(failed.status === "failed"
          ? "Повторная сверка не завершилась. Показана последняя сохранённая сверка."
          : "");
      } catch (error) {
        if (!active) return;
        setNotice(error instanceof ApiError && error.status === 404
          ? failed.message || "Не удалось подготовить сверку"
          : error instanceof Error ? error.message : "Не удалось восстановить актуальную сверку");
      }
    };
    const receive = (next: ReviewJob) => {
      if (!active) return;
      setReviewJob(next);
      if (next.status === "completed") {
        const analysisVersionId = next.analysis_version_id ?? next.output_version_id ?? undefined;
        setNotice("");
        void api<ProgramRead>(`/api/v1/programs/${session.programId}`)
          .then((latest) => {
            if (active) applyProgramState(latest, analysisVersionId);
          })
          .catch((error: unknown) => {
            if (active) setNotice(error instanceof Error ? error.message : "Не удалось обновить версию программы");
          });
      } else if ((next.status === "failed" || next.status === "conflict") && next.purpose === "prepare") {
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
          if (error instanceof ApiError && error.status === 404) {
            // Сверка исчезла вместе с программой (БД пересоздали).
            resetStaleSession();
            return;
          }
          setNotice(error instanceof Error ? error.message : "Не удалось получить состояние сверки");
          retryTimer = setTimeout(connect, 5000);
        });
    };
    connect();
    return () => { active = false; clearTimeout(retryTimer); stream?.close(); };
  }, [session.programId, session.reviewId, applyProgramState, resetStaleSession]);

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
        if (error instanceof ApiError && error.status === 404) {
          // Задание проверки относилось к исчезнувшей версии; молча забываем его.
          setCheckJob(null);
          updateSession({ checkJobId: undefined, checkVersionId: undefined });
          return;
        }
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
        if (error instanceof ApiError && error.status === 404) {
          // Проверка правила относилась к исчезнувшей версии; забываем её.
          const rest = { ...(sessionRef.current.optionalRuleCheckJobIds ?? {}) };
          delete rest[ruleCode];
          updateSession({ optionalRuleCheckJobIds: Object.keys(rest).length ? rest : undefined });
          setOptionalRuleChecks((current) => {
            const next = { ...current };
            delete next[ruleCode];
            return next;
          });
          return;
        }
        setNotice(error instanceof Error ? error.message : `Не удалось получить результат проверки «${ruleDisplayLabel(ruleCode).toLocaleLowerCase("ru")}»`);
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
      // Полная перезагрузка сверки занимает ~10 секунд на большом пакете, и
      // ждать её после каждого решения невозможно. Отмечаем решение локально,
      // а актуальные данные догружаем в фоне: следующий клик не блокируется.
      setReviewDetails((details) => details && {
        ...details,
        issues: details.issues.map((issue) => issue.id === conflictId
          ? { ...issue, decision: { variant_id: variantId, author: signature } }
          : issue),
      });
      void loadReviewDetails(version.id, current.programId).catch(() => {
        setNotice("Выбор сохранён, обновляем список расхождений…");
      });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось сохранить выбранное значение");
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function decideCustom(conflictId: string, value: string) {
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
        body: JSON.stringify({ expected_version_id: current.versionId, conflict_id: conflictId, custom_value: value, signature }),
      });
      updateSession({ versionId: version.id, checkJobId: undefined, checkVersionId: undefined });
      setCheckJob(null);
      setNotice("");
      setReviewDetails((details) => details && {
        ...details,
        issues: details.issues.map((issue) => issue.id === conflictId
          ? { ...issue, decision: { variant_id: `custom:${conflictId}`, author: signature } }
          : issue),
      });
      void loadReviewDetails(version.id, current.programId).catch(() => {
        setNotice("Выбор сохранён, обновляем список расхождений…");
      });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось сохранить собственный вариант");
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function decideLevelOnlyVariants(): Promise<number | null> {
    const current = sessionRef.current;
    if (reviewBusyRef.current || !current.programId || !current.reviewId || !current.versionId) return null;
    const details = reviewDetails;
    if (!details) return 0;

    const targets = levelOnlyVariantTargets(details.issues);

    if (targets.length === 0) return 0;
    const signature = signatureForProgram(signatureRef.current, current.programId);
    if (!signature) {
      setNotice("Укажите подпись для этой программы кнопкой над программой.");
      return null;
    }

    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const request: DecisionBatchRequest = {
        expected_version_id: current.versionId,
        selections: targets.map(({ conflictId, variantId }) => ({
          conflict_id: conflictId,
          variant_id: variantId,
        })),
        signature,
      };
      const version = await api<VersionRead>(
        `/api/v1/programs/${current.programId}/reviews/${current.reviewId}/decisions/batch`,
        {
          method: "POST",
          headers: operationHeaders(),
          body: JSON.stringify(request),
        },
      );
      updateSession({ versionId: version.id, checkJobId: undefined, checkVersionId: undefined });
      setCheckJob(null);
      const selectedByConflictId = new Map<string, string>();
      for (const { conflictId, variantId } of targets) {
        selectedByConflictId.set(conflictId, variantId);
      }
      setReviewDetails((latest) => latest && ({
        ...latest,
        version: {
          ...latest.version,
          id: version.id,
          number: version.number,
          snapshot: {
            ...version.snapshot,
            source_refs: version.snapshot.source_refs.length
              ? version.snapshot.source_refs
              : latest.version.snapshot.source_refs,
          },
        },
        issues: latest.issues.map((issue) => {
          const variantId = selectedByConflictId.get(issue.id);
          return variantId
            ? { ...issue, decision: { variant_id: variantId, author: signature } }
            : issue;
        }),
        confirmed: false,
        confirmation: null,
        check_job_id: null,
        check_report: null,
      }));
      setNotice("");
      return targets.length;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось сохранить выбранные формулировки");
      return null;
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function acceptChatProposal(
    sessionId: string,
    turnId: string,
    expectedVersionId: string,
  ): Promise<ChatProposalAcceptance> {
    const current = sessionRef.current;
    const reject = (message: string): ChatProposalAcceptance => {
      setNotice(message);
      return { accepted: false, message };
    };
    if (reviewBusyRef.current || !current.programId || !current.versionId || !current.reviewId) {
      return reject("Не удалось применить план: программа ещё не готова к изменениям.");
    }
    if (current.versionId !== expectedVersionId) {
      return reject("Версия программы изменилась. Обновите предложение в чате перед применением.");
    }
    const signature = signatureForProgram(signatureRef.current, current.programId);
    if (!signature) {
      return reject("Укажите подпись для этой программы кнопкой над программой.");
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
      try {
        await loadReviewDetails(accepted.version.id, current.programId);
      } catch (error) {
        setNotice(error instanceof Error
          ? `План применён, но не удалось обновить данные на экране: ${error.message}`
          : "План применён, но не удалось обновить данные на экране.");
      }
      return { accepted: true, appliedVersionId: accepted.version.id };
    } catch (error) {
      return reject(error instanceof Error ? error.message : "Не удалось применить предложение агента");
    } finally {
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function undoChatProposal(
    chatSessionId: string,
    turnId: string,
    expectedCurrentVersionId: string,
  ): Promise<ChatProposalUndo> {
    const current = sessionRef.current;
    const reject = (message: string): ChatProposalUndo => {
      setNotice(message);
      return { restored: false, message };
    };
    if (reviewBusyRef.current || !current.programId || !current.versionId || !current.reviewId) {
      return reject("Не удалось отменить изменения: программа ещё не готова.");
    }
    if (current.versionId !== expectedCurrentVersionId) {
      return reject("Версия программы уже изменилась. Обновите чат перед отменой.");
    }
    const signature = signatureForProgram(signatureRef.current, current.programId);
    if (!signature) {
      return reject("Укажите подпись для этой программы кнопкой над программой.");
    }
    reviewBusyRef.current = true;
    setReviewBusy(true);
    try {
      const restored = await api<VersionRead>(
        `/api/v1/programs/${current.programId}/chat/sessions/${chatSessionId}/runs/${turnId}/undo`,
        {
          method: "POST",
          headers: operationHeaders(),
          body: JSON.stringify({ expected_version_id: expectedCurrentVersionId, signature }),
        },
      );
      updateSession({
        versionId: restored.id,
        reviewId: restored.snapshot.review?.review_id,
        analysisVersionId: restored.snapshot.review?.analysis_version_id,
        checkJobId: undefined,
        checkVersionId: undefined,
        prepareKey: undefined,
      });
      setProgram((latest) => latest?.id === current.programId
        ? { ...latest, current_version_id: restored.id } : latest);
      // Сохраняем уже показанный граф и чат, пока читаем сводку восстановленной версии.
      // Иначе экран кратко показывает пустой чат и неверный номер версии по умолчанию.
      setCheckJob(null);
      setGraphBuildJob(null);
      setNotice("Изменения отменены. Восстановлена предыдущая версия программы.");
      return { restored: true, versionId: restored.id };
    } catch (error) {
      return reject(error instanceof Error ? error.message : "Не удалось отменить изменения");
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

  const optionalChecksReady = Boolean(
    reviewJob?.status === "completed" &&
    reviewJob.review_id === session.reviewId &&
    reviewDetails?.version.id === session.versionId &&
    reviewDetails?.version.snapshot.review?.review_id === session.reviewId,
  );

  async function runOptionalRuleCheck(ruleCode: OptionalRuleCode, target: string) {
    const current = sessionRef.current;
    const currentDetails = reviewDetails;
    const anotherCheckRunning = Object.values(optionalRuleChecks).some(
      (check) => check && ["pending", "processing"].includes(check.status),
    );
    const standardCheckRunning = Boolean(
      checkJob && ["pending", "processing"].includes(checkJob.status),
    );
    if (
      !target || !optionalChecksReady || reviewBusyRef.current || anotherCheckRunning || standardCheckRunning ||
      !current.programId || !current.reviewId || !current.versionId ||
      !currentDetails || currentDetails.version.id !== current.versionId ||
      currentDetails.version.snapshot.review?.review_id !== current.reviewId ||
      currentDetails.version.snapshot.review?.graph_ready !== true
    ) return;
    reviewBusyRef.current = true;
    setReviewBusy(true);
    setOptionalRuleLaunch({ status: "submitting", ruleCode, versionId: current.versionId });
    const isCurrentVersion = () => sessionRef.current.programId === current.programId &&
      sessionRef.current.reviewId === current.reviewId && sessionRef.current.versionId === current.versionId;
    try {
      const next = await api<ReviewJob>(
        `/api/v1/programs/${current.programId}/reviews/${current.reviewId}/checks`,
        {
          method: "POST",
          headers: operationHeaders(),
          body: JSON.stringify({
            version_id: current.versionId,
            rule_codes: [ruleCode],
            ...(ruleCode === "R-11" ? { document_ids: [target] } : { competency_code: target }),
          }),
        },
      );
      if (!isCurrentVersion()) return;
      const optionalRuleCheckJobIds = {
        ...(sessionRef.current.optionalRuleCheckJobIds ?? {}),
        [ruleCode]: next.id,
      };
      setOptionalRuleChecks((checks) => ({
        ...checks,
        [ruleCode]: { ...next, content_hash: "", report: null },
      }));
      updateSession({ optionalRuleCheckJobIds });
      setNotice("");
    } catch (error) {
      if (isCurrentVersion()) {
        const message = error instanceof Error ? error.message : "Не удалось запустить проверку";
        setOptionalRuleLaunch({ status: "failed", ruleCode, versionId: current.versionId, message });
      }
    } finally {
      setOptionalRuleLaunch((launch) => launch?.status === "submitting" ? null : launch);
      reviewBusyRef.current = false;
      setReviewBusy(false);
    }
  }

  async function confirm(acceptedWarningIds: string[], expectedVersionId?: string) {
    const current = sessionRef.current;
    if (expectedVersionId && current.versionId !== expectedVersionId) {
      setNotice("Версия программы изменилась. Просмотрите актуальные замечания.");
      return null;
    }
    if (reviewBusyRef.current || !current.programId || !current.reviewId || !current.versionId ||
      !checkJob || checkJob.status !== "completed" || checkJob.input_version_id !== current.versionId
    ) return null;
    const signature = signatureForProgram(signatureRef.current, current.programId);
    if (!signature) {
      setNotice("Укажите подпись для этой программы кнопкой над программой.");
      return null;
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
      void loadReviewDetails(version.id, current.programId).catch(() => {
        setNotice("Версия подтверждена. Не удалось обновить данные на экране.");
      });
      return version;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось подтвердить версию");
      return null;
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

  const sessionVerified = Boolean(
    session.programId && session.jobId &&
    recoveredReviewFor === `${session.programId}:${session.jobId}`,
  );
  return {
    programId: session.programId,
    sessionVerified,
    openingProgram,
    versionId: session.versionId ?? "",
    program,
    signature: signatureForProgram(signature, session.programId),
    signatureSaving,
    saveSignature,
    job,
    reviewJob,
    reviewDetails,
    graphBuildJob,
    checkJob,
    optionalRuleChecks,
    optionalChecksReady,
    optionalRuleLaunch: optionalRuleLaunch?.versionId === session.versionId ? optionalRuleLaunch : null,
    busy,
    reviewBusy,
    notice,
    canPrepareReview,
    clearNotice: () => setNotice(""),
    upload,
    retry,
    retryPrepare,
    decide,
    decideCustom,
    decideLevelOnlyVariants,
    acceptChatProposal,
    undoChatProposal,
    runCheck,
    runOptionalRuleCheck,
    confirm,
  };
}
