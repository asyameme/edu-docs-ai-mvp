import assert from "node:assert/strict";
import { test } from "node:test";

const details = (overrides = {}) => ({
  version: { id: "current", snapshot: { review: { lifecycle_policy: "automatic_source_v2" } } },
  lifecycle: { saved: true, can_generate: true, is_source: true, ...overrides },
  issues: [{ severity: "warning", status: "open", requires_decision: true }],
  confirmed: false,
});

test("сохранённая версия с открытыми замечаниями не требует confirm", async () => {
  const { automaticGenerationReview } = await import("../src/versionLifecycle.ts");
  const state = automaticGenerationReview(details(), "current");
  assert.deepEqual(state, { kind: "saved", versionId: "current" });
  assert.equal("confirm" in state, false);
});

test("проверка другой версии или отсутствие разрешения сервера блокируют пакет", async () => {
  const { automaticGenerationReview } = await import("../src/versionLifecycle.ts");
  assert.equal(automaticGenerationReview(details(), "changed").kind, "blocked");
  const pending = automaticGenerationReview(
    details({ can_generate: false, generation_reason: "Проверка выполняется" }), "current",
  );
  assert.equal(pending.kind, "blocked");
  assert.equal(pending.reason, "Проверка выполняется");
  assert.equal("confirm" in pending, false);
});

test("версия без маркера политики использует тот же процесс", async () => {
  const { automaticGenerationReview } = await import("../src/versionLifecycle.ts");
  const state = details();
  delete state.version.snapshot.review.lifecycle_policy;
  assert.deepEqual(automaticGenerationReview(state, "current"), { kind: "saved", versionId: "current" });
  assert.equal(automaticGenerationReview(null, "current").kind, "blocked");
});
