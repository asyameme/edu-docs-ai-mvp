import type { ImportJob, ReviewIssue } from "./api";

export type Diagnostic = {
  id: string;
  origin: "parser" | "validation";
  severity: "error" | "warning" | "info";
  category: string;
  ruleCode: string;
  title: string;
  message: string;
  documents: Array<{ id: string; name: string }>;
  detail: string | null;
  sourceRefIds?: string[];
};

export type DiagnosticGroup = {
  id: string;
  origin: Diagnostic["origin"];
  severity: Diagnostic["severity"];
  ruleCode: string;
  title: string;
  items: Diagnostic[];
  documents: Diagnostic["documents"];
};

export function ruleDisplayLabel(ruleCode: string): string {
  switch (ruleCode) {
    case "R-01": return "Состав и коды компетенций";
    case "R-01-graph": return "Связи компетенций между документами";
    case "R-02": return "Часы в учебном плане и рабочих программах";
    case "R-03-RPP": return "Нагрузка практики по семестрам";
    case "R-04": return "Часы и зачётные единицы";
    case "R-05-title": return "Зачётные единицы в титуле и нагрузке";
    case "R-07": return "Формулировки компетенций";
    case "R-08":
    case "R-08-semantic": return "Формулировки индикаторов";
    case "R-08-local-reference": return "Связи индикаторов и компетенций";
    case "R-09": return "Покрытие компетенций учебным планом";
    case "R-11": return "Результаты обучения и содержание дисциплин";
    case "Парсер": return "Разбор документов";
    default: return "Проверка документов";
  }
}

export function groupDiagnostics(items: Diagnostic[]): DiagnosticGroup[] {
  const groups = new Map<string, DiagnosticGroup>();
  const documents = new Map<string, Map<string, Diagnostic["documents"][number]>>();
  for (const item of items) {
    const title = item.title.normalize("NFKC").replace(/\s+/gu, " ").trim();
    const id = JSON.stringify([item.origin, item.severity, item.ruleCode, item.category, title]);
    const existing = groups.get(id);
    if (existing) existing.items.push(item);
    else {
      groups.set(id, { id, origin: item.origin, severity: item.severity,
        ruleCode: item.ruleCode, title, items: [item], documents: [] });
      documents.set(id, new Map());
    }
    for (const document of item.documents) documents.get(id)?.set(document.id, document);
  }
  const severityOrder = { error: 0, warning: 1, info: 2 };
  return [...groups.values()].map((group) => ({
    ...group, documents: [...(documents.get(group.id)?.values() ?? [])],
  })).sort((left, right) => severityOrder[left.severity] - severityOrder[right.severity]
    || (left.origin === right.origin ? 0 : left.origin === "validation" ? -1 : 1)
    || left.ruleCode.localeCompare(right.ruleCode, "ru", { numeric: true }));
}

const alignmentTitles: Record<string, string> = {
  rpd_section_2_missing: "Не извлечено содержание раздела 2",
  rpd_section_3_evidence_missing: "Не найдены связанные данные раздела 3",
  rpd_multiple_disciplines_unsupported: "Неоднозначна привязка содержания к дисциплине",
  rpd_outcome_id_ambiguous: "Результат обучения не удалось однозначно определить",
  rpd_outcome_competency_link_missing: "Не найдена связь результата с компетенцией",
  rpd_semantic_model_unavailable: "Смысловую проверку не удалось выполнить",
  rpd_semantic_response_invalid: "Ответ смысловой проверки требует проверки",
  rpd_semantic_result_missing_or_duplicate: "Модель не вернула однозначный результат проверки",
  rpd_semantic_unexpected_outcome_ids: "Ответ содержит неизвестные результаты обучения",
  rpd_semantic_alignment_gap: "Возможный пробел в содержании или оценивании",
  rpd_semantic_alignment_uncertain: "Связь требует экспертной оценки",
  rpd_semantic_evidence_missing: "Не хватает подтверждений для вывода",
};

export function reviewDiagnostic(issue: ReviewIssue, documents: Diagnostic["documents"]): Diagnostic {
  const alignment = issue.rule_code === "R-11" ? issue.rpd_alignment : null;
  return {
    id: issue.id, origin: "validation",
    severity: issue.severity === "blocking" ? "error" : "warning",
    category: issue.type, ruleCode: issue.rule_code,
    title: issue.rule_code === "R-11" ? alignmentTitles[issue.type] ?? issue.message : issue.message,
    message: issue.message, documents,
    sourceRefIds: issue.source_ref_ids,
    detail: alignment ? [
      alignment.outcome_text ? `Результат обучения: ${alignment.outcome_text}` : alignment.outcome_id,
      alignment.reason ? `Пояснение: ${alignment.reason}` : null,
    ].filter(Boolean).join("\n") || null : null,
  };
}

export function importDiagnostics(job: Pick<ImportJob, "files" | "issues">): Diagnostic[] {
  const names = new Map(job.files.map((file) => [file.document_id, file.member_name]));
  return job.issues.map((issue, index) => ({
    id: `import-${index}`, origin: "parser", severity: issue.severity,
    category: issue.category, ruleCode: "Парсер", title: parserDiagnosticTitle(issue.message), message: issue.message,
    documents: issue.document_id ? [{ id: issue.document_id,
      name: names.get(issue.document_id) ?? "Документ пакета" }] : [],
    detail: null,
  }));
}

function parserDiagnosticTitle(message: string): string {
  if (message.startsWith("УП: нет кодов компетенций для ")) {
    return "В учебном плане у дисциплин не найдены коды компетенций";
  }
  if (message.startsWith("УП: группа выбора не является отдельной дисциплиной:")) {
    return "Группы выбора в учебном плане требуют отдельного разбора";
  }
  if (message.startsWith("УП: название ") && message.includes("встречается в ")) {
    return "Повторяются названия дисциплин в строках учебного плана";
  }
  if (message.startsWith("Оценочные средства: название ")
    && message.endsWith("явная связь с индикатором не найдена")) {
    return "Для оценочных средств не найдены явные связи с индикаторами";
  }
  if (message.startsWith("Содержание: не удалось прочитать часы ")) {
    return "Не удалось прочитать часы для тем занятий";
  }
  if (message.startsWith("Содержание: пустое поле hours;")) {
    return "Пропущены строки содержания с незаполненными часами";
  }
  if (message.startsWith("Содержание: пустое поле activity;")) {
    return "Пропущены строки содержания с незаполненным видом занятия";
  }
  if (message.startsWith("Оценочные средства: индикатор ")
    && message.endsWith("связь с отсутствующим объектом не создана")) {
    return "Не извлечены индикаторы, на которые ссылаются оценочные средства";
  }
  if (message.startsWith("Найдено несколько траекторий или форм обучения;")) {
    return "Неоднозначно определена нагрузка для траекторий или форм обучения";
  }
  return message;
}
