import assert from "node:assert/strict";
import { test } from "node:test";

const ready = { live: true, graphReady: true, analysisComplete: true, pendingChoices: 3 };

test("раннее сохранение графа не пропускает экран выбора", async () => {
  const { reviewStageFor } = await import("../src/reviewStage.ts");
  assert.equal(reviewStageFor(ready), "choices");
  assert.equal(reviewStageFor({ ...ready, pendingChoices: 0 }), "results");
});

test("можно перейти к замечаниям с открытыми решениями и вернуться", async () => {
  const { reviewStageFor } = await import("../src/reviewStage.ts");
  assert.equal(reviewStageFor({ ...ready, selectedStage: "results" }), "results");
  assert.equal(reviewStageFor({ ...ready, pendingChoices: 0, selectedStage: "choices" }), "choices");
});

test("незавершённый анализ с пустым списком не открывает результаты", async () => {
  const { reviewStageFor } = await import("../src/reviewStage.ts");
  assert.equal(reviewStageFor({ ...ready, analysisComplete: false, pendingChoices: 0 }), "choices");
});

test("демонстрационный экран переключается по готовности графа", async () => {
  const { reviewStageFor } = await import("../src/reviewStage.ts");
  assert.equal(reviewStageFor({ ...ready, live: false, graphReady: false, selectedStage: "results" }), "choices");
  assert.equal(reviewStageFor({ ...ready, live: false }), "results");
});

test("последний сохранённый выбор открывает результаты только своей сверки", async () => {
  const { finishedChoosing } = await import("../src/reviewStage.ts");
  assert.equal(finishedChoosing({ key: "review-1", count: 1 }, { key: "review-1", count: 0 }), true);
  assert.equal(finishedChoosing({ key: "review-1", count: 2 }, { key: "review-1", count: 1 }), false);
  assert.equal(finishedChoosing({ key: "review-1", count: 1 }, { key: "review-2", count: 0 }), false);
  assert.equal(finishedChoosing({ key: "review-1", count: 0 }, { key: "review-1", count: 0 }), false);
});
