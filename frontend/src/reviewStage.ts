export type ReviewStage = "choices" | "results";

export function finishedChoosing(
  previous: { key: string; count: number },
  current: { key: string; count: number },
): boolean {
  return previous.key === current.key && previous.count > 0 && current.count === 0;
}

export function reviewStageFor(state: {
  live: boolean;
  graphReady: boolean;
  analysisComplete: boolean;
  pendingChoices: number;
  selectedStage?: ReviewStage;
}): ReviewStage {
  // Готовность исходного графа не завершает выбор исправлений.
  if (!state.live) return state.graphReady ? "results" : "choices";
  if (!state.analysisComplete) return "choices";
  return state.selectedStage ?? (state.pendingChoices > 0 ? "choices" : "results");
}
