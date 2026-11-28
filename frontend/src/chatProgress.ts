export type ChatProgress = { message: string };

export function parseChatProgress(value: unknown): ChatProgress | null {
  if (!value || typeof value !== "object" || !("message" in value)
    || typeof value.message !== "string" || !value.message.trim()) return null;
  return { message: value.message };
}
