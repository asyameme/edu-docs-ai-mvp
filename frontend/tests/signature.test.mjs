import assert from "node:assert/strict";
import { test } from "node:test";
import { signatureForProgram } from "../src/signature.ts";

const program = { id: "program-a" };

test("Подпись текущей программы возвращается как есть", () => {
  const signature = { programId: program.id, value: "Анна" };
  assert.equal(signatureForProgram(signature, program.id), "Анна");
});

test("Подпись другой программы не используется", () => {
  const foreign = { programId: "program-b", value: "Борис" };
  assert.equal(signatureForProgram(foreign, program.id), null);
});

test("Без подписи возвращается null", () => {
  assert.equal(signatureForProgram(null, program.id), null);
});
