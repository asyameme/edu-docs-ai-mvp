import { ruleDisplayLabel, type DiagnosticGroup } from "./diagnostics.ts";

const discussionRequest = "Разбери выбранное замечание по результатам проверки программы и помоги с его устранением. Контекст выбранного замечания:";

export function diagnosticDiscussionText(text: string): string {
  if (!text.startsWith(`${discussionRequest}\n\n`)) return text;
  try {
    const context: unknown = JSON.parse(text.slice(discussionRequest.length).trim());
    if (!context || typeof context !== "object" || !("title" in context)
      || typeof context.title !== "string" || !("documents" in context) || !Array.isArray(context.documents)) return text;
    const names = context.documents.flatMap((document: unknown) => (
      document && typeof document === "object" && "name" in document && typeof document.name === "string"
        ? [document.name] : []
    ));
    return `Разбери замечание проверки и помоги с его устранением:\n\n${context.title}`
      + (names.length ? `\n\nДокументы:\n${names.join("\n")}` : "");
  } catch { return text; }
}

export function diagnosticDiscussionPrompt(group: DiagnosticGroup, context: {
  programId: string; versionId: string; versionNumber: number;
}): string {
  const documents = (items: DiagnosticGroup["documents"]) => items.slice(0, 8).map((item) => ({
    id: item.id, name: item.name.slice(0, 240),
  }));
  const data = {
    program_id: context.programId, version_id: context.versionId, version_number: context.versionNumber,
    rule_code: group.ruleCode, rule: ruleDisplayLabel(group.ruleCode), origin: group.origin,
    severity: group.severity, title: group.title.slice(0, 1200),
    records_total: group.items.length, documents_total: group.documents.length,
    documents: documents(group.documents),
    records: group.items.slice(0, 20).map((item) => ({
      id: item.id, category: item.category, message: item.message.slice(0, 1200),
      detail: item.detail?.slice(0, 1200) ?? null, documents_total: item.documents.length,
      documents: documents(item.documents),
      source_ref_ids: (item.sourceRefIds ?? []).slice(0, 12),
      source_refs_total: item.sourceRefIds?.length ?? 0,
    })),
  };
  const format = () => `${discussionRequest}\n\n${JSON.stringify(data, null, 2)}`;
  // Большие группы догружаются инструментами агента; сообщение укладывается в лимит API.
  while (format().length > 11000 && data.records.length > 1) data.records.pop();
  return format();
}
