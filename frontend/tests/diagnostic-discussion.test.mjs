import assert from "node:assert/strict";
import { test } from "node:test";
import { groupDiagnostics } from "../src/diagnostics.ts";

const issue = (id, name = "РПД.docx") => ({
  id, origin: "validation", severity: "warning", category: "ambiguous_link",
  ruleCode: "R-01-graph", title: "Неоднозначная связь", message: "Найдено две подходящие строки",
  detail: "Семестры 2 и 5", documents: [{ id: `doc-${id}`, name }], sourceRefIds: [`source-${id}`],
});
const context = { programId: "program-1", versionId: "version-3", versionNumber: 3 };

test("запрос передаёт только выбранную группу, все её записи и документы", async () => {
  const { diagnosticDiscussionPrompt } = await import("../src/diagnosticDiscussion.ts");
  const groups = groupDiagnostics([issue("one"), issue("two", "УП.docx"), { ...issue("other"), title: "Другая проблема" }]);
  const selected = groups.find((group) => group.title === "Неоднозначная связь");
  const prompt = diagnosticDiscussionPrompt(selected, context);
  for (const value of ["program-1", "version-3", "R-01-graph", "one", "two", "РПД.docx", "УП.docx", "Семестры 2 и 5", "source-one", "source-two"]) {
    assert.ok(prompt.includes(value), value);
  }
  assert.ok(!prompt.includes("Другая проблема"));
  assert.ok(!prompt.includes("doc-other"));
  assert.match(prompt, /по результатам проверки программы/);
  assert.ok(!prompt.includes("задай уточняющий вопрос"));
  assert.ok(!prompt.includes("Не выбирай неоднозначную связь"));
});

test("большая группа помещается в лимит сообщения без скрытой потери объёма", async () => {
  const { diagnosticDiscussionPrompt } = await import("../src/diagnosticDiscussion.ts");
  const group = groupDiagnostics(Array.from({ length: 200 }, (_, i) => ({
    ...issue(`id-${i}`, "д".repeat(3000)), message: "м".repeat(5000), detail: "п".repeat(5000),
  })))[0];
  const prompt = diagnosticDiscussionPrompt(group, context);
  assert.ok(prompt.length <= 12000);
  assert.match(prompt, /"records_total": 200/);
  assert.match(prompt, /"documents_total": 200/);
  assert.ok(prompt.includes("id-0"));
  const data = JSON.parse(prompt.slice(prompt.indexOf("{\n")));
  assert.ok(data.records.length > 0 && data.records.length < 200);
});

test("в чате и после восстановления истории виден понятный текст без служебных данных", async () => {
  const { diagnosticDiscussionPrompt, diagnosticDiscussionText } = await import("../src/diagnosticDiscussion.ts");
  const prompt = diagnosticDiscussionPrompt(groupDiagnostics([issue("one")])[0], context);
  const visible = diagnosticDiscussionText(prompt);
  assert.match(visible, /Неоднозначная связь/);
  assert.match(visible, /РПД.docx/);
  for (const hidden of ["program_id", "source_ref_ids", "doc-one", "version-3", "{", "}"]) {
    assert.ok(!visible.includes(hidden), hidden);
  }
  assert.equal(diagnosticDiscussionText('Обычный запрос с JSON {"title":"пример"}'), 'Обычный запрос с JSON {"title":"пример"}');
  assert.equal(diagnosticDiscussionText("Помоги исправить часы"), "Помоги исправить часы");
});
