import { useState } from "react";
import { groupDiagnostics, ruleDisplayLabel, type Diagnostic, type DiagnosticGroup } from "./diagnostics";

const severityLabels = { error: "Ошибка", warning: "Предупреждение", info: "Информация" };
const PAGE_SIZE = 12;

export type DiagnosticDiscussion = {
  onDiscuss: (group: DiagnosticGroup) => void;
  disabled: boolean;
};

function DocumentDiagnostics({ items }: { items: Diagnostic[] }) {
  const [expanded, setExpanded] = useState(false);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const names = items[0]?.documents.map((document) => document.name).join(" · ") || "Пакет документов";
  return <li>
    <details onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary className="diagnostic-sources">{names} · Записей: {items.length}</summary>
      {expanded && <>
        <ul className="diagnostic-records">{items.slice(0, limit).map((item) => <li key={item.id}>
          <p>{item.message}</p>
          {item.detail && <p className="diagnostic-detail">{item.detail}</p>}
        </li>)}</ul>
        {items.length > limit && <button type="button" className="secondary-button"
          onClick={() => setLimit((current) => current + PAGE_SIZE)}>
          Ещё записи · осталось {items.length - limit}
        </button>}
      </>}
    </details>
  </li>;
}

function DiagnosticGroupCard({ group, query, discussion }: {
  group: DiagnosticGroup; query: string; discussion?: DiagnosticDiscussion;
}) {
  const [expanded, setExpanded] = useState(false);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const byDocument = new Map<string, Diagnostic[]>();
  for (const item of group.items) {
    const key = JSON.stringify(item.documents.map((document) => document.id).sort());
    const existing = byDocument.get(key);
    if (existing) existing.push(item);
    else byDocument.set(key, [item]);
  }
  const documentGroups = [...byDocument.entries()].filter(([, items]) => !query
    || group.title.toLocaleLowerCase("ru").includes(query)
    || group.ruleCode.toLocaleLowerCase("ru").includes(query)
    || items.some((item) => [item.message, item.detail ?? "", ...item.documents.map((document) => document.name)]
      .some((value) => value.toLocaleLowerCase("ru").includes(query))));
  return (
    <li className={`diagnostic-group diagnostic-group--${group.severity}${discussion ? " diagnostic-group--discussable" : ""}`}>
      <details onToggle={(event) => setExpanded(event.currentTarget.open)}>
        <summary>
          <span className={`severity-dot severity-dot--${group.severity}`} aria-hidden="true" />
          <span className="diagnostic-group-copy">
            <strong>{group.title}</strong>
            <span>{ruleDisplayLabel(group.ruleCode)} · {severityLabels[group.severity]}
              {group.documents.length > 0 ? ` · Документов: ${group.documents.length}` : ""}
              {group.items.length > 1 ? ` · Повторений: ${group.items.length}` : ""}</span>
          </span>
        </summary>
        {expanded && (
          <div className="diagnostic-group-details">
            {group.origin === "validation" && group.ruleCode === "R-11" && <p>
              Смысловую оценку проверяет специалист. Отсутствие извлечённых данных требует проверки парсинга.
            </p>}
            <ul>{documentGroups.slice(0, limit).map(([id, items]) => (
              <DocumentDiagnostics key={id} items={items} />
            ))}</ul>
            {documentGroups.length > limit && <button type="button" className="secondary-button"
              onClick={() => setLimit((current) => current + PAGE_SIZE)}>
              Ещё документы · осталось {documentGroups.length - limit}
            </button>}
          </div>
        )}
      </details>
      {discussion && <button className="secondary-button diagnostic-discuss-button" type="button"
        disabled={discussion.disabled} aria-label={`Обсудить с агентом: ${group.title}`}
        onClick={() => discussion.onDiscuss(group)}>
        Обсудить с агентом
      </button>}
    </li>
  );
}

export function DiagnosticList({ items, discussion }: {
  items: Diagnostic[]; discussion?: DiagnosticDiscussion;
}) {
  const [severity, setSeverity] = useState<"action" | "all" | Diagnostic["severity"]>("action");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const groups = groupDiagnostics(items);
  const counts = { error: 0, warning: 0, info: 0 };
  for (const group of groups) counts[group.severity] += 1;
  const search = query.trim().toLocaleLowerCase("ru");
  const visible = groups.filter((group) => (severity === "all"
    || (severity === "action" ? group.severity !== "info" : group.severity === severity))
    && (!search || [group.title, group.ruleCode, ...group.items.flatMap((item) => [
      item.message, item.detail ?? "", ...item.documents.map((document) => document.name),
    ])].some((value) => value.toLocaleLowerCase("ru").includes(search))));
  return (
    <div className="diagnostics">
      <p className="diagnostic-summary">
        Замечаний: {groups.length}. Повторяющиеся сообщения объединены.
      </p>
      <div className="diagnostic-toolbar">
        <label>Показать
          <select value={severity} onChange={(event) => {
            const value = event.target.value;
            if (value === "action" || value === "all" || value === "error" || value === "warning" || value === "info") {
              setSeverity(value); setLimit(PAGE_SIZE);
            }
          }}>
            <option value="action">Ошибки и предупреждения · {counts.error + counts.warning}</option>
            <option value="error">Ошибки · {counts.error}</option>
            <option value="warning">Предупреждения · {counts.warning}</option>
            <option value="info">Информация · {counts.info}</option>
            <option value="all">Все · {groups.length}</option>
          </select>
        </label>
        <label>Поиск по тексту или документу
          <input type="search" value={query} placeholder="Название проверки или документа"
            onChange={(event) => { setQuery(event.target.value); setLimit(PAGE_SIZE); }} />
        </label>
      </div>
      {visible.length === 0 && <p className="competency-empty-issues">
        {counts.info > 0 && severity === "action"
          ? "Ошибок и предупреждений нет. Информационные сообщения доступны в фильтре."
          : "По выбранному фильтру замечаний нет."}
      </p>}
      <ul className="diagnostic-groups">{visible.slice(0, limit).map((group) => (
        <DiagnosticGroupCard key={group.id} group={group} query={search} discussion={discussion} />
      ))}</ul>
      {visible.length > limit && <button type="button" className="secondary-button"
        onClick={() => setLimit((current) => current + PAGE_SIZE)}>
        Показать ещё замечания · осталось {visible.length - limit}
      </button>}
    </div>
  );
}
