import assert from "node:assert/strict";
import { test } from "node:test";
import { parseChatProgress } from "../src/chatProgress.ts";

test("принимает понятный текст и последовательность действий", () => {
  assert.deepEqual(parseChatProgress({ message: "Читаю документ…", steps: ["Ищу компетенции…"] }),
    { message: "Читаю документ…", steps: ["Ищу компетенции…"] });
});

test("игнорирует неверные события и ограничивает историю действий", () => {
  for (const value of [null, [], {}, { message: 1 }, { message: " " }]) {
    assert.equal(parseChatProgress(value), null);
  }
  const result = parseChatProgress({ message: "Работаю…", steps: [null, ...Array(20).fill("Читаю…")] });
  assert.equal(result.steps.length, 8);
});
