import assert from "node:assert/strict";
import { test } from "node:test";
import { levelOnlyVariantTargets } from "../src/levelVariants.ts";

const PREFIX = "Выбрать значение из документов: ";

test("пара с ведущим дефисом списка и уровнем закрывается кнопкой", () => {
  const targets = levelOnlyVariantTargets([{
    id: "conflict-1",
    requires_decision: true,
    decision: null,
    variants: [
      { id: "v1", label: PREFIX + "- Разрабатывает масштабируемый и поддерживаемый код" },
      { id: "v2", label: PREFIX + "Разрабатывает масштабируемый и поддерживаемый код (Продвинутый)" },
    ],
  }]);

  assert.deepEqual(targets, [{ conflictId: "conflict-1", variantId: "v2" }]);
});

test("обычная уровневая пара закрывается вариантом с уровнем", () => {
  const targets = levelOnlyVariantTargets([{
    id: "conflict-2",
    requires_decision: true,
    decision: null,
    variants: [
      { id: "v1", label: PREFIX + "Анализирует задачу, выделяя ее базовые составляющие" },
      { id: "v2", label: PREFIX + "анализирует задачу, выделяя её базовые составляющие (Средний)" },
    ],
  }]);

  assert.deepEqual(targets, [{ conflictId: "conflict-2", variantId: "v2" }]);
});

test("содержательно разные формулировки и решённые вопросы не трогаются", () => {
  const targets = levelOnlyVariantTargets([
    {
      id: "conflict-3",
      requires_decision: true,
      decision: null,
      variants: [
        { id: "v1", label: PREFIX + "Планирует эксперимент" },
        { id: "v2", label: PREFIX + "Проводит эксперимент (Базовый)" },
      ],
    },
    {
      id: "conflict-4",
      requires_decision: true,
      decision: { variant_id: "done" },
      variants: [
        { id: "v1", label: PREFIX + "Совпадает" },
        { id: "v2", label: PREFIX + "Совпадает (Базовый)" },
      ],
    },
    {
      id: "conflict-5",
      requires_decision: true,
      decision: null,
      variants: [
        { id: "v1", label: PREFIX + "Обе без уровня А" },
        { id: "v2", label: PREFIX + "Обе без уровня Б" },
      ],
    },
  ]);

  assert.deepEqual(targets, []);
});
