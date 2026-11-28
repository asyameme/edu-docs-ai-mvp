import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { waitForChatRun } from "../src/chat-run.ts";

const originalEventSource = globalThis.EventSource;

class FakeEventSource extends EventTarget {
  static CLOSED = 2;
  static last;
  readyState = 1;
  closed = false;

  constructor(url) {
    super();
    this.url = url;
    FakeEventSource.last = this;
  }

  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(name, data = {}) {
    this.dispatchEvent(new MessageEvent(name, { data: JSON.stringify(data) }));
  }
}

afterEach(() => { globalThis.EventSource = originalEventSource; });

function start(onCancelling = () => {}) {
  globalThis.EventSource = FakeEventSource;
  const controller = new AbortController();
  const result = waitForChatRun("program", "run", controller.signal, onCancelling);
  return { controller, result, stream: FakeEventSource.last };
}

test("остановленный запрос закрывает поток и игнорирует поздний ответ", async () => {
  const { result, stream } = start();
  stream.emit("cancelled");
  stream.emit("completed", { kind: "answer", message: "Поздний ответ", sources: [] });
  assert.deepEqual(await result, { status: "cancelled" });
  assert.equal(stream.closed, true);
});

test("при смене запроса ожидание прерывается и старый поток закрывается", async () => {
  const { controller, result, stream } = start();
  controller.abort();
  await assert.rejects(result, { name: "AbortError" });
  stream.emit("completed", {});
  assert.equal(stream.closed, true);
  const next = start();
  next.stream.emit("completed", { kind: "answer", message: "Новый ответ", sources: [] });
  assert.equal((await next.result).result.message, "Новый ответ");
});

test("запрос остановки ещё не считается завершённой остановкой", async () => {
  let stopping = false;
  const { result, stream } = start(() => { stopping = true; });
  stream.emit("status", { status: "cancelling" });
  assert.equal(stopping, true);
  assert.equal(stream.closed, false);
  stream.emit("cancelled");
  assert.deepEqual(await result, { status: "cancelled" });
});

test("потеря статуса предлагает остановку вместо ложного завершения", async () => {
  const { result, stream } = start();
  stream.emit("unavailable");
  await assert.rejects(result, /Можно остановить/);
  assert.equal(stream.closed, true);
});

test("таймаут закрывает поток и предлагает остановить запрос", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const { result, stream } = start();
  context.mock.timers.tick(5 * 60 * 1000);
  await assert.rejects(result, /Можно остановить запрос и отправить следующий/);
  assert.equal(stream.closed, true);
});
