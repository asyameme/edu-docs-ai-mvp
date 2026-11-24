// Выбор «формулировки с уровнем»: чистые функции без React, тестируются node:test.
export type LevelVariantIssue = {
  id: string;
  requires_decision: boolean;
  decision?: { variant_id: string } | null;
  variants: Array<{ id: string; label: string }>;
};

// Уровень освоения («Базовый», «Средний», …) в скобках — служебная пометка ЦКМВ:
// одни документы её приводят, другие нет, содержание совпадает.
const LEVEL_NOTE = /[([{]\s*(?:базовый|средний|продвинутый|пороговый|повышенный)\s*[)\]}]/i;
const LEVEL_NOTE_ALL = /[([{]\s*(?:базовый|средний|продвинутый|пороговый|повышенный)\s*[)\]}]/gi;
// Ведущий маркер списка («- », «— », «• ») и краевая пунктуация — оформление,
// не содержание; сравниваем формулировки без них.
const LEADING_BULLET = /^[-–—•·▪●*]+\s*/;
const TRAILING_PUNCTUATION = /[.,;:!?]+$/;

function variantLabelText(label: string): string {
  return label.replace(/^Выбрать значение из документов:\s*/i, "").trim();
}

function variantBaseText(label: string): string {
  return variantLabelText(label)
    .replace(LEVEL_NOTE_ALL, "")
    .replace(LEADING_BULLET, "")
    .replaceAll("ё", "е")
    .toLocaleLowerCase("ru")
    .replace(/\s+/g, " ")
    .trim()
    .replace(TRAILING_PUNCTUATION, "")
    .replace(LEADING_BULLET, "")
    .trim();
}

function variantHasLevel(label: string): boolean {
  return LEVEL_NOTE.test(variantLabelText(label));
}

export function levelOnlyVariantTargets(issues: LevelVariantIssue[]): Array<{
  conflictId: string;
  variantId: string;
}> {
  const targets: Array<{ conflictId: string; variantId: string }> = [];
  for (const issue of issues) {
    if (!issue.requires_decision || issue.decision || issue.variants.length < 2) continue;

    const bases = new Set(issue.variants.map((variant) => variantBaseText(variant.label)));
    if (bases.size !== 1) continue;

    const withLevel = issue.variants.filter((variant) => variantHasLevel(variant.label));
    if (withLevel.length !== 1) continue;

    targets.push({ conflictId: issue.id, variantId: withLevel[0].id });
  }
  return targets;
}
