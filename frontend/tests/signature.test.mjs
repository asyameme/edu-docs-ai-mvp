import assert from "node:assert/strict";
import { test } from "node:test";
import { requestSignature, signatureForProgram } from "../src/signature.ts";

const program = { id: "program-a", name: "Программа А" };

function withPrompt(answer, run) {
  const calls = [];
  globalThis.window = {
    prompt: (...args) => { calls.push(args); return answer; },
  };
  // Обращение к старому общему хранилищу недопустимо.
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() { throw new Error("Общая подпись не должна читаться из браузера"); },
  });
  try { run(calls); }
  finally { delete globalThis.window; delete globalThis.localStorage; }
}

test("Запрос называет программу и сохраняет её ID вместе с именем", () => {
  withPrompt("  Анна  ", (calls) => {
    assert.deepEqual(requestSignature(program), {
      status: "accepted", signature: { programId: program.id, value: "Анна" },
    });
    assert.ok(calls[0][0].includes(program.name));
    assert.equal(calls[0][1], "");
  });
});

test("Подпись другой программы не используется ни в действии, ни в запросе имени", () => {
  const previous = { programId: "program-b", value: "Борис" };
  assert.equal(signatureForProgram(previous, program.id), null);
  withPrompt("Анна", (calls) => {
    requestSignature(program, previous);
    assert.equal(calls[0][1], "");
  });
});

test("Смена имени предлагает только подпись текущей программы", () => {
  withPrompt("Борис", (calls) => {
    const result = requestSignature(program, { programId: program.id, value: "Анна" });
    assert.equal(calls[0][1], "Анна");
    assert.equal(signatureForProgram(result.signature, program.id), "Борис");
  });
});

test("При следующем открытии нет общего кеша имени предыдущего пользователя", () => {
  withPrompt("Анна", () => requestSignature(program));
  withPrompt("Борис", (calls) => {
    const result = requestSignature(program);
    assert.equal(calls[0][1], "");
    assert.equal(result.signature.value, "Борис");
  });
});

test("Отмена смены подписи не меняет прежнее значение", () => {
  const previous = Object.freeze({ programId: program.id, value: "Анна" });
  withPrompt(null, () => {
    assert.deepEqual(requestSignature(program, previous), { status: "cancelled" });
    assert.equal(signatureForProgram(previous, program.id), "Анна");
  });
  assert.equal(signatureForProgram(null, program.id), null);
});

for (const value of ["", "   ", "я".repeat(121)]) {
  test(`Недопустимая подпись отклоняется: длина ${value.length}`, () => {
    withPrompt(value, () => assert.deepEqual(requestSignature(program), { status: "invalid" }));
  });
}

test("Граница 120 символов совпадает с правилом Unicode бэкенда", () => {
  withPrompt("😀".repeat(120), () => assert.equal(requestSignature(program).status, "accepted"));
  withPrompt("😀".repeat(121), () => assert.equal(requestSignature(program).status, "invalid"));
});

test("Имя invalid является обычной подписью, а не кодом ошибки", () => {
  withPrompt("invalid", () => assert.equal(requestSignature(program).signature.value, "invalid"));
});
