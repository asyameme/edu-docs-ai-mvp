import { useEffect, useRef, useState } from "react";
import { api, ApiError, loadSession, saveSession, type ImportJob } from "./api";

export function useImportJob() {
  const initial = useRef(loadSession());
  const busyRef = useRef(false);
  const [programId, setProgramId] = useState(initial.current?.programId || "");
  const [jobId, setJobId] = useState(initial.current?.jobId || "");
  const [job, setJob] = useState<ImportJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!programId || !jobId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let stream: EventSource | undefined;
    const connect = async () => {
      try {
        const next = await api<ImportJob>(`/api/v1/programs/${programId}/imports/${jobId}`);
        if (!active) return;
        setJob(next);
        setNotice("");
        if (["pending", "processing"].includes(next.status)) {
          stream = new EventSource(`/api/v1/programs/${programId}/imports/${jobId}/events`);
          stream.addEventListener("status", event => {
            if (!active) return;
            const updated = JSON.parse((event as MessageEvent).data) as ImportJob;
            setJob(updated);
            setNotice("");
            if (!["pending", "processing"].includes(updated.status)) stream?.close();
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
  }, [programId, jobId, job?.attempt]);

  async function upload(file: File): Promise<boolean> {
    if (busyRef.current) return false;
    if (!/\.zip$/i.test(file.name)) {
      setNotice("Выберите один ZIP-архив с документами DOCX/XLSX.");
      return false;
    }
    if (file.size > 50 * 1024 * 1024) {
      setNotice("Размер архива не должен превышать 50 МиБ.");
      return false;
    }
    busyRef.current = true;
    setBusy(true);
    setNotice("Отправляем архив на сервер…");
    try {
      let target = programId;
      if (target) {
        try { await api(`/api/v1/programs/${target}`); }
        catch (error) {
          if (error instanceof ApiError && error.status === 404) target = "";
          else throw error;
        }
      }
      if (!target) {
        const created = await api<{ id: string }>("/api/v1/programs", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: file.name.replace(/\.zip$/i, "") || "Программа" }),
        });
        target = created.id;
        setProgramId(target);
        saveSession(target, "");
      }
      const data = new FormData();
      data.append("file", file);
      const accepted = await api<ImportJob>(`/api/v1/programs/${target}/imports`, {
        method: "POST", body: data, headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      setJob(accepted);
      setJobId(accepted.id);
      setNotice("");
      saveSession(target, accepted.id);
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
      const next = await api<ImportJob>(`/api/v1/programs/${programId}/imports/${job.id}/retry`, {
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

  return { programId, job, busy, notice, clearNotice: () => setNotice(""), upload, retry };
}
