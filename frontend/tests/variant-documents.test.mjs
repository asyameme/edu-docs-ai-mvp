import assert from "node:assert/strict";
import { test } from "node:test";
import { variantDocumentIds } from "../src/variantDocuments.ts";

const change = (document_id, expected_old_value, value) => ({
  op: "set_document_value", document_id, expected_old_value, value, field_path: "/indicators/0/text",
});

test("показывает источники формулировки, а не все документы назначения", () => {
  const changes = [change("a", "Первый текст", "Первый текст"),
    change("b", "Другой текст", "Первый текст"), change("a", "Первый текст", "Первый текст")];
  assert.deepEqual(variantDocumentIds({ changes }, true), ["a"]);
  assert.deepEqual(variantDocumentIds({ changes: changes.map(c => ({ ...c, value: "Другой текст" })) }, true), ["b"]);
});

test("учитывает нормализацию пробелов, регистра, ё и в т. ч., сохраняя уровень", () => {
  assert.deepEqual(variantDocumentIds({ changes: [
    change("a", "-  УЧЁТ в т. ч. данных.", "учет в том числе данных"),
    change("b", "Учет в том числе данных (Базовый)", "учет в том числе данных"),
  ] }, true), ["a"]);
});

test("для кодов использует исходное значение без нормализации формулировок", () => {
  assert.deepEqual(variantDocumentIds({ changes: [change("a", "УК-1", "УК-1"),
    change("b", "УК-2", "УК-1")] }, false), ["a"]);
});
