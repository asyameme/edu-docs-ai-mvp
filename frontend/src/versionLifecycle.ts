import type { ReviewDetails } from "./api";
import type { GenerationReview } from "./GenerationScreen";

export function automaticGenerationReview(
  details: ReviewDetails | null,
  currentVersionId: string,
): GenerationReview {
  if (!details || details.version.id !== currentVersionId) {
    return { kind: "blocked", reason: "Обновляем сохранённую версию…" };
  }
  if (!details.lifecycle?.saved || !details.lifecycle.can_generate) {
    return {
      kind: "blocked",
      reason: details.lifecycle?.generation_reason ?? "Ожидаем результаты проверки этой версии.",
    };
  }
  return { kind: "saved", versionId: currentVersionId };
}
