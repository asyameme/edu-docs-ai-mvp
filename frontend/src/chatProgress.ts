export type ChatProgress = { message: string; steps: string[] };

export function parseChatProgress(value: unknown): ChatProgress | null {
  if (!value || typeof value !== "object" || !("message" in value)
    || typeof value.message !== "string" || !value.message.trim()) return null;
  const steps = "steps" in value && Array.isArray(value.steps)
    ? value.steps.filter((step): step is string => typeof step === "string").slice(-8) : [];
  return { message: value.message, steps };
}
