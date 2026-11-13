-- Запускает владелец БД через psql -v runtime_user=имя_существующего_логина.
-- Логин приложения создаётся отдельно и не должен владеть таблицами или иметь SUPERUSER.
BEGIN;
GRANT USAGE ON SCHEMA public TO :"runtime_user";
GRANT SELECT ON programs,program_versions,graph_states,entity_revisions,relation_revisions,
    graph_entities,graph_relations,source_documents,graph_documents,document_artifacts,
    source_refs,revision_sources,source_links,graph_sources,graph_source_links,
    processing_jobs,validation_issues,generated_packages,change_log TO :"runtime_user";
GRANT INSERT ON programs,program_versions,graph_states,entity_revisions,relation_revisions,
    graph_entities,graph_relations,source_documents,graph_documents,document_artifacts,
    source_refs,revision_sources,source_links,graph_sources,graph_source_links,
    processing_jobs,validation_issues,generated_packages,change_log TO :"runtime_user";
GRANT UPDATE ON programs,processing_jobs,graph_states,graph_entities,graph_relations,
    graph_documents,graph_sources,graph_source_links TO :"runtime_user";
GRANT DELETE ON graph_states,graph_entities,graph_relations,graph_documents,
    graph_sources,graph_source_links TO :"runtime_user";
GRANT SELECT,INSERT ON chat_sessions,chat_turns TO :"runtime_user";
GRANT UPDATE ON chat_sessions,chat_turns TO :"runtime_user";
COMMIT;
