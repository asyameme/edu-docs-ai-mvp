import assert from "node:assert/strict";
import { test } from "node:test";
import { parseChatProgress } from "../src/chatProgress.ts";

test("принимает текущий статус без дублирующей истории действий", () => {
  assert.deepEqual(parseChatProgress({ message: "Читаю документ…", steps: ["Ищу компетенции…"] }),
    { message: "Читаю документ…" });
});

test("игнорирует неверные события и лишнюю историю действий", () => {
  for (const value of [null, [], {}, { message: 1 }, { message: " " }]) {
    assert.equal(parseChatProgress(value), null);
  }
  const result = parseChatProgress({ message: "Работаю…", steps: [null, ...Array(20).fill("Читаю…")] });
  assert.deepEqual(result, { message: "Работаю…" });
});

test("повторные чтения не накапливаются, следующий этап заменяет текущий", () => {
  const reading = parseChatProgress({ message: "Читаю фрагмент документа…",
    steps: Array(8).fill("Читаю фрагмент документа…") });
  assert.deepEqual(reading, { message: "Читаю фрагмент документа…" });
  assert.deepEqual(parseChatProgress({ message: "Анализирую результаты и готовлю ответ…",
    steps: Array(8).fill("Читаю фрагмент документа…") }),
    { message: "Анализирую результаты и готовлю ответ…" });
});
