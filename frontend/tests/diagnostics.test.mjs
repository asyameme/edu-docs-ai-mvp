import assert from "node:assert/strict";
import { test } from "node:test";
import { groupDiagnostics, importDiagnostics, reviewDiagnostic, updateAcceptedWarnings } from "../src/diagnostics.ts";

const issue = {
  id: "i", rule_code: "R-01", type: "competency_not_in_program_catalogue",
  severity: "warning", message: "Код A не найден в справочниках", source_ref_ids: [],
};

test("Тысячи повторов объединяются с сохранением всех записей и документов", () => {
  const items = Array.from({ length: 2318 }, (_, index) => reviewDiagnostic(
    { ...issue, id: `issue-${index}` }, [{ id: `doc-${index % 78}`, name: "РПД.docx" }],
  ));
  const groups = groupDiagnostics(items);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].items.length, 2318);
  assert.equal(groups[0].documents.length, 78);
});

test("Разные коды, правила и уровни важности сохраняются отдельными группами", () => {
  const items = [issue, { ...issue, id: "b", message: "Код B не найден в справочниках" },
    { ...issue, id: "c", rule_code: "R-08-local-reference" },
    { ...issue, id: "d", severity: "blocking" }].map((item) => reviewDiagnostic(item, []));
  const groups = groupDiagnostics(items);
  assert.equal(groups.length, 4);
  assert.equal(groups[0].severity, "error");
});

test("Отсутствие раздела R-11 становится общей проблемой, все результаты доступны внутри", () => {
  const items = Array.from({ length: 90 }, (_, index) => reviewDiagnostic({
    ...issue, id: `outcome-${index}`, rule_code: "R-11", type: "rpd_section_2_missing",
    message: `В документе ${index % 3} нет содержания раздела 2`,
    rpd_alignment: { outcome_text: `Результат ${index}`, reason: "Нет данных раздела 2" },
  }, [{ id: `doc-${index % 3}`, name: `РПД-${index % 3}.docx` }]));
  const groups = groupDiagnostics(items);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].documents.length, 3);
  assert.ok(groups[0].items[89].detail.includes("Результат 89"));
});

test("Повторяющаяся информация парсера не превращается в предупреждение", () => {
  const items = importDiagnostics({
    files: [{ document_id: "a", member_name: "a.docx" }, { document_id: "b", member_name: "b.docx" }],
    issues: [
      { category: "manual_review", severity: "info", message: "Вычислены часы", document_id: "a" },
      { category: "manual_review", severity: "info", message: "Вычислены часы", document_id: "b" },
      { category: "manual_review", severity: "warning", message: "Вычислены часы", document_id: "a" },
    ],
  });
  const groups = groupDiagnostics(items);
  assert.equal(groups.length, 2);
  assert.equal(groups[1].severity, "info");
  assert.equal(groups[1].documents.length, 2);
});

test("Подтверждение группы передаёт каждый ID и не принимает другие группы", () => {
  const current = ["other", "a"];
  const accepted = updateAcceptedWarnings(current, ["a", "b", "c"], true);
  assert.deepEqual(new Set(accepted), new Set(["other", "a", "b", "c"]));
  assert.deepEqual(current, ["other", "a"]);
  assert.deepEqual(updateAcceptedWarnings(accepted, ["a", "b", "c"], false), ["other"]);
});

test("Отсутствующие коды у разных дисциплин УП показываются как одна проблема", () => {
  const issues = ["Китайский язык", "Хинди"].map((name) => ({
    category: "empty_cell", severity: "warning", document_id: "plan",
    message: `УП: нет кодов компетенций для '${name}'; связи не созданы`,
  }));
  const groups = groupDiagnostics(importDiagnostics({ files: [], issues }));
  assert.equal(groups.length, 1);
  assert.equal(groups[0].items.length, 2);
  assert.ok(groups[0].items[1].message.includes("Хинди"));
});
