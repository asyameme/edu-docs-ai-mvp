import { parseChatProgress, type ChatProgress } from "./chatProgress.ts";

export type ChatRunEnd =
  | { status: "completed"; result: unknown }
  | { status: "failed" | "cancelled" };

export function waitForChatRun(
  programId: string,
  runId: string,
  signal: AbortSignal,
  onCancelling: () => void,
  onProgress: (progress: ChatProgress) => void = () => {},
): Promise<ChatRunEnd> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Ожидание остановлено", "AbortError"));
      return;
    }
    const stream = new EventSource(`/api/v1/programs/${programId}/chat/runs/${runId}/events`);
    let settled = false;
    let timeout: ReturnType<typeof setTimeout>;
    const finish = (error: Error | null, result?: ChatRunEnd) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
      stream.close();
      if (error) reject(error);
      else if (result) resolve(result);
    };
    const abort = () => finish(new DOMException("Ожидание остановлено", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    stream.addEventListener("progress", (event) => {
      if (settled) return;
      try {
        const progress = parseChatProgress(JSON.parse((event as MessageEvent).data));
        if (progress) onProgress(progress);
      } catch {}
    });
    stream.addEventListener("completed", (event) => {
      try {
        finish(null, { status: "completed", result: JSON.parse((event as MessageEvent).data) });
      } catch {
        finish(new Error("Не удалось разобрать ответ агента."));
      }
    });
    stream.addEventListener("cancelled", () => finish(null, { status: "cancelled" }));
    stream.addEventListener("failed", () => finish(null, { status: "failed" }));
    stream.addEventListener("status", (event) => {
      try {
        if (JSON.parse((event as MessageEvent).data).status === "cancelling") onCancelling();
      } catch {
        finish(new Error("Не удалось прочитать состояние запроса."));
      }
    });
    stream.addEventListener("unavailable", () => finish(new Error(
      "Состояние запроса временно недоступно. Можно остановить запрос или обновить страницу.",
    )));
    stream.addEventListener("error", () => {
      if (stream.readyState === EventSource.CLOSED) finish(new Error("Соединение с чатом потеряно. Можно остановить запрос."));
    });
    timeout = setTimeout(() => finish(new Error(
      "Агент не ответил за отведённое время. Можно остановить запрос и отправить следующий.",
    )), 5 * 60 * 1000);
  });
}
