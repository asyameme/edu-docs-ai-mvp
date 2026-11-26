import type { ReviewVariant } from "./api";

function normalizedFormulation(value: string): string {
  return value.normalize("NFKC").toLocaleLowerCase("ru").replaceAll("ё", "е")
    .replace(/\s+/gu, " ")
    .replace(/(?<![\p{L}\p{N}_])в\s+т\s*\.\s*ч\s*\.?(?=\s|$|[,;:])/gu, "в том числе")
    .trim().replace(/^\p{P}+|\p{P}+$/gu, "").trim();
}

export function variantDocumentIds(variant: ReviewVariant, formulations: boolean): string[] {
  // Изменения включают все целевые документы; источники варианта определяет прежнее значение.
  return [...new Set(variant.changes.flatMap((change) => {
    if (change.op !== "set_document_value") return [];
    const old = change.expected_old_value;
    const chosen = change.value;
    const matches = formulations && typeof old === "string" && typeof chosen === "string"
      ? normalizedFormulation(old) === normalizedFormulation(chosen)
      : old === chosen;
    return matches ? [change.document_id] : [];
  }))];
}
