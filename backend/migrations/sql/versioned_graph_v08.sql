-- Проект пустой БД v0.8 для согласования; не миграция приложения.
CREATE TABLE "programs" (
  "id" uuid PRIMARY KEY,
  "name" text NOT NULL CHECK (length(trim(name))>0),
  "current_version_id" uuid,
  "created_at" timestamptz NOT NULL DEFAULT (now())
);

CREATE TABLE "program_versions" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "number" bigint NOT NULL CHECK (number>0),
  "graph_state_id" uuid NOT NULL,
  "parent_version_id" uuid,
  "restored_from_version_id" uuid,
  "origin" text NOT NULL CHECK (origin IN ('initial','save','restore')),
  "review" jsonb CHECK (review IS NULL OR jsonb_typeof(review)='object'),
  "operation_key" text,
  "request_hash" text,
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  CHECK ((operation_key IS NULL)=(request_hash IS NULL))
);

CREATE TABLE "graph_states" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "schema_version" smallint NOT NULL CHECK (schema_version IN (1,2)) DEFAULT 2,
  "metadata" jsonb NOT NULL CHECK (jsonb_typeof(metadata)='object') DEFAULT ('{}'::jsonb),
  "producing_job_id" uuid,
  "producing_attempt" integer CHECK (producing_attempt>0),
  "status" text NOT NULL CHECK (status IN ('building','sealed')) DEFAULT 'building',
  "content_hash" text CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  "sealed_at" timestamptz,
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  -- NULL попытки допустим для восстановленного исторического результата с известной задачей.
  CHECK (producing_attempt IS NULL OR producing_job_id IS NOT NULL),
  CHECK ((status='building' AND sealed_at IS NULL AND content_hash IS NULL) OR (status='sealed' AND sealed_at IS NOT NULL AND content_hash IS NOT NULL))
);

CREATE TABLE "entity_revisions" (
  "revision_id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "entity_id" uuid NOT NULL,
  "kind" text NOT NULL CHECK (length(trim(kind))>0),
  "attributes" jsonb NOT NULL CHECK (jsonb_typeof(attributes)='object'),
  "created_at" timestamptz NOT NULL DEFAULT (now())
);

CREATE TABLE "relation_revisions" (
  "revision_id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "relation_id" uuid NOT NULL,
  "source_entity_id" uuid NOT NULL,
  "target_entity_id" uuid NOT NULL,
  "type" text NOT NULL CHECK (length(trim(type))>0),
  "confidence" double precision CHECK (confidence>=0 AND confidence<=1),
  "attributes" jsonb NOT NULL CHECK (jsonb_typeof(attributes)='object') DEFAULT ('{}'::jsonb),
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  CHECK (NOT (attributes ? 'matrix_value') OR attributes->'matrix_value'='null'::jsonb OR (type='FORMS_COMPETENCY' AND attributes->'matrix_value' IN ('0'::jsonb,'1'::jsonb)))
);

CREATE TABLE "graph_entities" (
  "program_id" uuid NOT NULL,
  "graph_state_id" uuid NOT NULL,
  "entity_id" uuid NOT NULL,
  "revision_id" uuid NOT NULL,
  "position" integer NOT NULL CHECK (position>=0),
  PRIMARY KEY ("graph_state_id", "entity_id")
);

CREATE TABLE "graph_relations" (
  "program_id" uuid NOT NULL,
  "graph_state_id" uuid NOT NULL,
  "relation_id" uuid NOT NULL,
  "revision_id" uuid NOT NULL,
  "source_entity_id" uuid NOT NULL,
  "target_entity_id" uuid NOT NULL,
  "position" integer NOT NULL CHECK (position>=0),
  PRIMARY KEY ("graph_state_id", "relation_id")
);

CREATE TABLE "source_documents" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "original_name" text NOT NULL,
  "format" text NOT NULL CHECK (format IN ('pdf','docx','xlsx')),
  "media_type" text NOT NULL,
  "size_bytes" bigint NOT NULL CHECK (size_bytes>0),
  "sha256" text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
  "content" bytea NOT NULL,
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  CHECK (octet_length(content)=size_bytes),
  CHECK (encode(sha256(content),'hex')=sha256)
);

CREATE TABLE "graph_documents" (
  "program_id" uuid NOT NULL,
  "graph_state_id" uuid NOT NULL,
  "document_id" uuid NOT NULL,
  "parsed_artifact_id" uuid,
  "parsed_artifact_kind" text NOT NULL CHECK (parsed_artifact_kind='parsed_json') DEFAULT 'parsed_json',
  "member_name" text NOT NULL,
  "position" integer NOT NULL CHECK (position>=0),
  PRIMARY KEY ("graph_state_id", "document_id")
);

CREATE TABLE "document_artifacts" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "document_id" uuid NOT NULL,
  "producing_job_id" uuid NOT NULL,
  "producing_attempt" integer CHECK (producing_attempt>0),
  "kind" text NOT NULL CHECK (kind IN ('markdown','parsed_json')),
  "parser_revision" text NOT NULL,
  "parser_config" jsonb NOT NULL CHECK (jsonb_typeof(parser_config)='object'),
  "parser_config_hash" text NOT NULL CHECK (parser_config_hash ~ '^[a-f0-9]{64}$'),
  "hash_encoding" text NOT NULL CHECK (hash_encoding IN ('jcs-rfc8785-utf8','raw-bytes')),
  "parsed_output" jsonb,
  "storage_key" text UNIQUE,
  "sha256" text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
  "size_bytes" bigint NOT NULL CHECK (size_bytes>=0),
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  CHECK ((kind='parsed_json' AND hash_encoding='jcs-rfc8785-utf8') OR (kind='markdown' AND hash_encoding='raw-bytes')),
  CHECK ((kind='parsed_json' AND parsed_output IS NOT NULL AND jsonb_typeof(parsed_output)='object' AND storage_key IS NULL) OR (kind='markdown' AND parsed_output IS NULL AND storage_key IS NOT NULL))
);

CREATE TABLE "source_refs" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "document_id" uuid NOT NULL,
  "artifact_id" uuid,
  "artifact_kind" text NOT NULL CHECK (artifact_kind='markdown') DEFAULT 'markdown',
  "section" text,
  "locator" text NOT NULL CHECK (length(trim(locator))>0),
  "artifact_locator" text,
  CHECK ((artifact_id IS NULL AND artifact_locator IS NULL) OR (artifact_id IS NOT NULL AND artifact_locator IS NOT NULL AND length(trim(artifact_locator))>0))
);

CREATE TABLE "revision_sources" (
  "program_id" uuid NOT NULL,
  "entity_revision_id" uuid,
  "relation_revision_id" uuid,
  "source_ref_id" uuid NOT NULL,
  "position" integer NOT NULL CHECK (position>=0),
  CHECK ((entity_revision_id IS NULL)<>(relation_revision_id IS NULL))
);

CREATE TABLE "source_links" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "entity_id" uuid,
  "relation_id" uuid,
  "source_ref_id" uuid NOT NULL,
  "field_path" text NOT NULL CHECK (length(trim(field_path))>0),
  "observed_value" jsonb NOT NULL CHECK (jsonb_typeof(observed_value)='object' AND observed_value ? 'value'),
  CHECK ((entity_id IS NULL)<>(relation_id IS NULL))
);

CREATE TABLE "graph_sources" (
  "program_id" uuid NOT NULL,
  "graph_state_id" uuid NOT NULL,
  "source_ref_id" uuid NOT NULL,
  "document_id" uuid NOT NULL,
  "position" integer NOT NULL CHECK (position>=0),
  PRIMARY KEY ("graph_state_id", "source_ref_id")
);

CREATE TABLE "graph_source_links" (
  "program_id" uuid NOT NULL,
  "graph_state_id" uuid NOT NULL,
  "source_link_id" uuid NOT NULL,
  "entity_id" uuid,
  "relation_id" uuid,
  "source_ref_id" uuid NOT NULL,
  "position" integer NOT NULL CHECK (position>=0),
  -- Публичный ID прежнего контракта отделён от ID неизменяемого наблюдения.
  "public_id" uuid NOT NULL,
  UNIQUE ("graph_state_id", "public_id"),
  CHECK ((entity_id IS NULL)<>(relation_id IS NULL)),
  PRIMARY KEY ("graph_state_id", "source_link_id")
);

CREATE TABLE "processing_jobs" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "input_version_id" uuid NOT NULL,
  "output_version_id" uuid,
  "checked_version_id" uuid,
  "input_graph_state_id" uuid,
  "output_graph_state_id" uuid,
  "operation_key" text NOT NULL,
  "request_hash" text NOT NULL,
  "type" text NOT NULL CHECK (type IN ('import','validate','generate')),
  "temporal_namespace" text NOT NULL,
  "workflow_id" text NOT NULL,
  "parameters" jsonb NOT NULL CHECK (jsonb_typeof(parameters)='object') DEFAULT ('{}'::jsonb),
  "report" jsonb NOT NULL CHECK (jsonb_typeof(report)='object') DEFAULT ('{}'::jsonb),
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  CHECK (checked_version_id IS NULL OR (type='validate' AND checked_version_id=COALESCE(output_version_id,input_version_id)))
);

CREATE TABLE "validation_issues" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "job_id" uuid NOT NULL,
  "version_id" uuid NOT NULL,
  "rule_code" text NOT NULL,
  "rule_revision" text NOT NULL,
  "type" text NOT NULL,
  "severity" text NOT NULL CHECK (severity IN ('warning','blocking')),
  "status" text NOT NULL CHECK (status IN ('open','needs_review')),
  "details" jsonb NOT NULL CHECK (jsonb_typeof(details)='object'),
  "created_at" timestamptz NOT NULL DEFAULT (now())
);

CREATE TABLE "generated_packages" (
  "job_id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "graph_state_id" uuid NOT NULL,
  "version_id" uuid,
  "import_job_id" uuid,
  "content" bytea NOT NULL,
  "size_bytes" bigint NOT NULL CHECK (size_bytes>0),
  "sha256" text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  CHECK ((version_id IS NULL)<>(import_job_id IS NULL)),
  CHECK (octet_length(content)=size_bytes),
  CHECK (encode(sha256(content),'hex')=sha256)
);

CREATE TABLE "change_log" (
  "id" uuid PRIMARY KEY,
  "program_id" uuid NOT NULL,
  "version_id" uuid NOT NULL,
  "position" integer NOT NULL CHECK (position>=0),
  "target_kind" text NOT NULL CHECK (target_kind IN ('entity','relation','graph','review')),
  "target_id" uuid NOT NULL,
  "field_path" text NOT NULL,
  "action" text NOT NULL CHECK (action IN ('add','replace','remove')),
  "old_value" jsonb,
  "new_value" jsonb,
  "actor_type" text NOT NULL CHECK (actor_type IN ('user','agent','system')),
  "actor_id" text NOT NULL CHECK (length(trim(actor_id))>0),
  "reason" text,
  "created_at" timestamptz NOT NULL DEFAULT (now()),
  CHECK ((action='add' AND old_value IS NULL AND new_value IS NOT NULL) OR (action='remove' AND old_value IS NOT NULL AND new_value IS NULL) OR (action='replace' AND old_value IS NOT NULL AND new_value IS NOT NULL AND old_value<>new_value))
);

CREATE UNIQUE INDEX ON "program_versions" ("program_id", "id");

CREATE UNIQUE INDEX ON "program_versions" ("program_id", "number");

CREATE UNIQUE INDEX ON "program_versions" ("program_id", "operation_key");

CREATE INDEX ON "program_versions" ("program_id", "graph_state_id");

CREATE INDEX ON "program_versions" ("program_id", "parent_version_id");

CREATE INDEX ON "program_versions" ("program_id", "restored_from_version_id");

CREATE UNIQUE INDEX ON "graph_states" ("program_id", "id");

CREATE UNIQUE INDEX ON "graph_states" ("program_id", "producing_job_id", "producing_attempt");

CREATE UNIQUE INDEX ON "entity_revisions" ("program_id", "revision_id");

CREATE UNIQUE INDEX ON "entity_revisions" ("program_id", "entity_id", "revision_id");

CREATE UNIQUE INDEX ON "relation_revisions" ("program_id", "revision_id");

CREATE UNIQUE INDEX "relation_revision_identity" ON "relation_revisions" ("program_id", "relation_id", "revision_id", "source_entity_id", "target_entity_id");

CREATE UNIQUE INDEX ON "graph_entities" ("graph_state_id", "position");

CREATE INDEX ON "graph_entities" ("program_id", "entity_id", "revision_id");

CREATE UNIQUE INDEX ON "graph_relations" ("graph_state_id", "position");

CREATE INDEX ON "graph_relations" ("graph_state_id", "source_entity_id");

CREATE INDEX ON "graph_relations" ("graph_state_id", "target_entity_id");

CREATE INDEX "graph_relation_revision_idx" ON "graph_relations" ("program_id", "relation_id", "revision_id", "source_entity_id", "target_entity_id");

CREATE UNIQUE INDEX ON "source_documents" ("program_id", "id");

CREATE UNIQUE INDEX ON "graph_documents" ("graph_state_id", "position");

CREATE INDEX ON "graph_documents" ("program_id", "document_id", "parsed_artifact_id", "parsed_artifact_kind");

CREATE UNIQUE INDEX ON "document_artifacts" ("program_id", "document_id", "id");

CREATE UNIQUE INDEX ON "document_artifacts" ("program_id", "document_id", "id", "kind");

CREATE UNIQUE INDEX "artifact_run_document_kind" ON "document_artifacts" ("program_id", "producing_job_id", "producing_attempt", "document_id", "kind");

CREATE UNIQUE INDEX ON "source_refs" ("program_id", "id");

CREATE UNIQUE INDEX ON "source_refs" ("program_id", "id", "document_id");

CREATE INDEX ON "source_refs" ("program_id", "document_id", "artifact_id", "artifact_kind");

CREATE UNIQUE INDEX ON "revision_sources" ("entity_revision_id", "position");

CREATE UNIQUE INDEX ON "revision_sources" ("relation_revision_id", "position");

CREATE INDEX ON "revision_sources" ("program_id", "source_ref_id");

CREATE UNIQUE INDEX ON "source_links" ("program_id", "id");

CREATE UNIQUE INDEX "source_link_entity_identity" ON "source_links" ("program_id", "id", "entity_id", "source_ref_id");

CREATE UNIQUE INDEX "source_link_relation_identity" ON "source_links" ("program_id", "id", "relation_id", "source_ref_id");

CREATE INDEX ON "source_links" ("program_id", "source_ref_id");

CREATE UNIQUE INDEX ON "graph_sources" ("graph_state_id", "position");

CREATE INDEX ON "graph_sources" ("graph_state_id", "document_id");

CREATE INDEX ON "graph_sources" ("program_id", "source_ref_id", "document_id");

CREATE UNIQUE INDEX ON "graph_source_links" ("graph_state_id", "position");

CREATE INDEX ON "graph_source_links" ("graph_state_id", "entity_id");

CREATE INDEX ON "graph_source_links" ("graph_state_id", "relation_id");

CREATE INDEX ON "graph_source_links" ("graph_state_id", "source_ref_id");

CREATE INDEX "graph_link_entity_idx" ON "graph_source_links" ("program_id", "source_link_id", "entity_id", "source_ref_id");

CREATE INDEX "graph_link_relation_idx" ON "graph_source_links" ("program_id", "source_link_id", "relation_id", "source_ref_id");

CREATE UNIQUE INDEX ON "processing_jobs" ("program_id", "id");

CREATE UNIQUE INDEX ON "processing_jobs" ("program_id", "id", "checked_version_id");

CREATE INDEX ON "processing_jobs" ("program_id", "checked_version_id");

CREATE UNIQUE INDEX ON "processing_jobs" ("program_id", "operation_key");

CREATE UNIQUE INDEX ON "processing_jobs" ("temporal_namespace", "workflow_id");

CREATE INDEX ON "processing_jobs" ("program_id", "input_version_id");

CREATE INDEX ON "processing_jobs" ("program_id", "output_version_id");

CREATE INDEX ON "processing_jobs" ("program_id", "input_graph_state_id");

CREATE INDEX ON "processing_jobs" ("program_id", "output_graph_state_id");

CREATE INDEX ON "validation_issues" ("program_id", "job_id", "version_id");

CREATE INDEX ON "validation_issues" ("program_id", "version_id");

CREATE INDEX ON "generated_packages" ("program_id", "graph_state_id");

CREATE INDEX ON "generated_packages" ("program_id", "version_id");

CREATE INDEX ON "generated_packages" ("program_id", "import_job_id");

CREATE UNIQUE INDEX ON "change_log" ("version_id", "position");

CREATE INDEX ON "change_log" ("program_id", "version_id");

CREATE INDEX ON "change_log" ("program_id", "target_kind", "target_id", "created_at");

COMMENT ON TABLE "programs" IS 'Программы и указатели на текущие версии.';

COMMENT ON TABLE "program_versions" IS 'История программы: состав графа, решения и подтверждение. Append-only: UPDATE и DELETE запрещены; только sealed-граф.';

COMMENT ON TABLE "graph_states" IS 'Состав одного состояния графа. После фиксации неизменяем; импорт ещё не обязан быть версией программы.';

COMMENT ON TABLE "entity_revisions" IS 'Неизменяемые редакции узлов. entity_id сохраняется, новая редакция получает новый revision_id.';

COMMENT ON TABLE "relation_revisions" IS 'Редакции направленных связей. Концы задают бизнес-ID узлов; их наличие проверяет состав состояния.';

COMMENT ON TABLE "graph_entities" IS 'Выбранные редакции узлов и порядок entities в SnapshotV2.';

COMMENT ON TABLE "graph_relations" IS 'Выбранные редакции связей; оба конца обязательно входят в это состояние.';

COMMENT ON TABLE "source_documents" IS 'Неизменяемые оригиналы документов.';

COMMENT ON TABLE "graph_documents" IS 'Документы состояния вместо version_documents; фиксирует порядок и результат разбора.';

COMMENT ON TABLE "document_artifacts" IS 'Для parsed_json размер и SHA-256 считаются от JCS RFC 8785 в UTF-8; для Markdown — от байтов файла.';

COMMENT ON TABLE "source_refs" IS 'Неизменяемые адреса в оригинале и Markdown; могут использоваться несколькими состояниями.';

COMMENT ON TABLE "revision_sources" IS 'Точный упорядоченный список source_ref_ids редакции; отдельно от свидетельств отдельных полей.';

COMMENT ON TABLE "source_links" IS 'Исходное значение поля: JSON Pointer либо $. Значение null сохраняется явно.';

COMMENT ON TABLE "graph_sources" IS 'Состав и порядок source_refs; документ каждого адреса включён в состояние.';

COMMENT ON TABLE "graph_source_links" IS 'Точный состав source_links. FK связывают свидетельство с целью и источником того же состояния.';

COMMENT ON TABLE "processing_jobs" IS 'Операции приложения: параметры, итог, выбранный граф и ключ повтора. Прогресс и история выполнения — в Temporal.';

COMMENT ON TABLE "validation_issues" IS 'Замечания только задачи validate и точно её checked_version_id; вход либо подготовленная выходная версия.';

COMMENT ON TABLE "generated_packages" IS 'Готовые ZIP с точным ID использованного состояния графа.';

COMMENT ON TABLE "change_log" IS 'Неизменяемый журнал: объект, поле, до/после, автор и версия. SQL NULL — отсутствие; JSON null — значение. Записывается вместе с версией.';

COMMENT ON COLUMN "change_log"."field_path" IS 'JSON Pointer; пустая строка означает объект целиком.';

ALTER TABLE "program_versions" ADD FOREIGN KEY ("program_id") REFERENCES "programs" ("id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "programs" ADD FOREIGN KEY ("id", "current_version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "program_versions" ADD FOREIGN KEY ("program_id", "parent_version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "program_versions" ADD FOREIGN KEY ("program_id", "restored_from_version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "program_versions" ADD FOREIGN KEY ("program_id", "graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_states" ADD FOREIGN KEY ("program_id") REFERENCES "programs" ("id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "entity_revisions" ADD FOREIGN KEY ("program_id") REFERENCES "programs" ("id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "relation_revisions" ADD FOREIGN KEY ("program_id") REFERENCES "programs" ("id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "source_documents" ADD FOREIGN KEY ("program_id") REFERENCES "programs" ("id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_states" ADD FOREIGN KEY ("program_id", "producing_job_id") REFERENCES "processing_jobs" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_entities" ADD FOREIGN KEY ("program_id", "graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_relations" ADD FOREIGN KEY ("program_id", "graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_documents" ADD FOREIGN KEY ("program_id", "graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_sources" ADD FOREIGN KEY ("program_id", "graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_source_links" ADD FOREIGN KEY ("program_id", "graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_entities" ADD FOREIGN KEY ("program_id", "entity_id", "revision_id") REFERENCES "entity_revisions" ("program_id", "entity_id", "revision_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_relations" ADD FOREIGN KEY ("program_id", "relation_id", "revision_id", "source_entity_id", "target_entity_id") REFERENCES "relation_revisions" ("program_id", "relation_id", "revision_id", "source_entity_id", "target_entity_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_relations" ADD FOREIGN KEY ("graph_state_id", "source_entity_id") REFERENCES "graph_entities" ("graph_state_id", "entity_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_relations" ADD FOREIGN KEY ("graph_state_id", "target_entity_id") REFERENCES "graph_entities" ("graph_state_id", "entity_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_documents" ADD FOREIGN KEY ("program_id", "document_id") REFERENCES "source_documents" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_documents" ADD FOREIGN KEY ("program_id", "document_id", "parsed_artifact_id", "parsed_artifact_kind") REFERENCES "document_artifacts" ("program_id", "document_id", "id", "kind") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "document_artifacts" ADD FOREIGN KEY ("program_id", "document_id") REFERENCES "source_documents" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "document_artifacts" ADD FOREIGN KEY ("program_id", "producing_job_id") REFERENCES "processing_jobs" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "source_refs" ADD FOREIGN KEY ("program_id", "document_id") REFERENCES "source_documents" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "source_refs" ADD FOREIGN KEY ("program_id", "document_id", "artifact_id", "artifact_kind") REFERENCES "document_artifacts" ("program_id", "document_id", "id", "kind") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "revision_sources" ADD FOREIGN KEY ("program_id", "entity_revision_id") REFERENCES "entity_revisions" ("program_id", "revision_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "revision_sources" ADD FOREIGN KEY ("program_id", "relation_revision_id") REFERENCES "relation_revisions" ("program_id", "revision_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "revision_sources" ADD FOREIGN KEY ("program_id", "source_ref_id") REFERENCES "source_refs" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "source_links" ADD FOREIGN KEY ("program_id", "source_ref_id") REFERENCES "source_refs" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_sources" ADD FOREIGN KEY ("program_id", "source_ref_id", "document_id") REFERENCES "source_refs" ("program_id", "id", "document_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_sources" ADD FOREIGN KEY ("graph_state_id", "document_id") REFERENCES "graph_documents" ("graph_state_id", "document_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_source_links" ADD FOREIGN KEY ("program_id", "source_link_id") REFERENCES "source_links" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_source_links" ADD FOREIGN KEY ("program_id", "source_link_id", "entity_id", "source_ref_id") REFERENCES "source_links" ("program_id", "id", "entity_id", "source_ref_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_source_links" ADD FOREIGN KEY ("graph_state_id", "entity_id") REFERENCES "graph_entities" ("graph_state_id", "entity_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_source_links" ADD FOREIGN KEY ("program_id", "source_link_id", "relation_id", "source_ref_id") REFERENCES "source_links" ("program_id", "id", "relation_id", "source_ref_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_source_links" ADD FOREIGN KEY ("graph_state_id", "relation_id") REFERENCES "graph_relations" ("graph_state_id", "relation_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "graph_source_links" ADD FOREIGN KEY ("graph_state_id", "source_ref_id") REFERENCES "graph_sources" ("graph_state_id", "source_ref_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "processing_jobs" ADD FOREIGN KEY ("program_id", "input_version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "processing_jobs" ADD FOREIGN KEY ("program_id", "output_version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "processing_jobs" ADD FOREIGN KEY ("program_id", "input_graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "processing_jobs" ADD FOREIGN KEY ("program_id", "output_graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "validation_issues" ADD FOREIGN KEY ("program_id", "job_id", "version_id") REFERENCES "processing_jobs" ("program_id", "id", "checked_version_id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "validation_issues" ADD FOREIGN KEY ("program_id", "version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "generated_packages" ADD FOREIGN KEY ("program_id", "job_id") REFERENCES "processing_jobs" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "generated_packages" ADD FOREIGN KEY ("program_id", "import_job_id") REFERENCES "processing_jobs" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "generated_packages" ADD FOREIGN KEY ("program_id", "version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "generated_packages" ADD FOREIGN KEY ("program_id", "graph_state_id") REFERENCES "graph_states" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "change_log" ADD FOREIGN KEY ("program_id", "version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE "processing_jobs" ADD FOREIGN KEY ("program_id", "checked_version_id") REFERENCES "program_versions" ("program_id", "id") DEFERRABLE INITIALLY IMMEDIATE;

-- Запись графа сериализуется в пределах программы. Внешние вызовы выполняются до транзакции.
CREATE FUNCTION graph_lock_program(p uuid) RETURNS void LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  PERFORM 1 FROM programs WHERE id=p FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Программа не найдена' USING ERRCODE='23503'; END IF;
END $$;

CREATE FUNCTION graph_reject_mutation() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  RAISE EXCEPTION 'История неизменяема: % %', TG_TABLE_NAME,TG_OP USING ERRCODE='23514';
END $$;

CREATE FUNCTION graph_guard_membership() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE p uuid; s uuid;
BEGIN
  IF TG_OP='DELETE' THEN p:=OLD.program_id; s:=OLD.graph_state_id;
  ELSE p:=NEW.program_id; s:=NEW.graph_state_id; END IF;
  PERFORM graph_lock_program(p);
  IF TG_OP='UPDATE' AND (NEW.program_id<>OLD.program_id OR NEW.graph_state_id<>OLD.graph_state_id)
  THEN RAISE EXCEPTION 'Перенос состава между состояниями запрещён' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM graph_states WHERE id=s AND program_id=p AND status='building')
  THEN RAISE EXCEPTION 'Состав можно менять только у building' USING ERRCODE='23514'; END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION graph_guard_revision_sources() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  PERFORM graph_lock_program(NEW.program_id);
  IF EXISTS(SELECT 1 FROM graph_entities g JOIN graph_states s ON s.id=g.graph_state_id
    WHERE g.revision_id=NEW.entity_revision_id AND s.status='sealed')
    OR EXISTS(SELECT 1 FROM graph_relations g JOIN graph_states s ON s.id=g.graph_state_id
    WHERE g.revision_id=NEW.relation_revision_id AND s.status='sealed')
  THEN RAISE EXCEPTION 'Нельзя дополнять источники запечатанной редакции' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION graph_guard_state() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP='DELETE' THEN
    PERFORM graph_lock_program(OLD.program_id);
    IF OLD.status='sealed' THEN RAISE EXCEPTION 'Удаление sealed запрещено' USING ERRCODE='23514'; END IF;
    RETURN OLD;
  END IF;
  PERFORM graph_lock_program(NEW.program_id);
  IF TG_OP='INSERT' AND NEW.status<>'building'
  THEN RAISE EXCEPTION 'Новое состояние начинается с building' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' THEN
    IF OLD.status='sealed' OR NEW.id<>OLD.id OR NEW.program_id<>OLD.program_id
    THEN RAISE EXCEPTION 'Изменение sealed или идентичности запрещено' USING ERRCODE='23514'; END IF;
  END IF;
  IF NEW.status='sealed' THEN
    IF EXISTS(
      SELECT 1 FROM revision_sources r WHERE r.program_id=NEW.program_id
      AND (EXISTS(SELECT 1 FROM graph_entities g WHERE g.graph_state_id=NEW.id AND g.revision_id=r.entity_revision_id)
        OR EXISTS(SELECT 1 FROM graph_relations g WHERE g.graph_state_id=NEW.id AND g.revision_id=r.relation_revision_id))
      AND NOT EXISTS(SELECT 1 FROM graph_sources g WHERE g.graph_state_id=NEW.id AND g.source_ref_id=r.source_ref_id)
    ) THEN RAISE EXCEPTION 'Источники редакций не включены в граф' USING ERRCODE='23514'; END IF;
    IF EXISTS(SELECT 1 FROM graph_documents g JOIN document_artifacts a ON a.id=g.parsed_artifact_id
      WHERE g.graph_state_id=NEW.id AND a.kind<>'parsed_json')
    THEN RAISE EXCEPTION 'Документ должен выбирать parsed_json' USING ERRCODE='23514'; END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION graph_require_sealed(p uuid,s uuid) RETURNS void LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF s IS NOT NULL AND NOT EXISTS(SELECT 1 FROM graph_states WHERE program_id=p AND id=s AND status='sealed')
  THEN RAISE EXCEPTION 'Ссылка допускается только на sealed-граф' USING ERRCODE='23514'; END IF;
END $$;

CREATE FUNCTION graph_guard_version() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  PERFORM graph_lock_program(NEW.program_id);
  PERFORM graph_require_sealed(NEW.program_id,NEW.graph_state_id);
  IF (NEW.origin='initial')<>(NEW.parent_version_id IS NULL)
     OR (NEW.origin='restore')<>(NEW.restored_from_version_id IS NOT NULL)
  THEN RAISE EXCEPTION 'Неверные ссылки истории версии' USING ERRCODE='23514'; END IF;
  IF NEW.origin='restore' AND NOT EXISTS(SELECT 1 FROM program_versions
     WHERE program_id=NEW.program_id AND id=NEW.restored_from_version_id AND graph_state_id=NEW.graph_state_id)
  THEN RAISE EXCEPTION 'Восстановление должно выбирать исходное состояние' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION graph_guard_job() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  PERFORM graph_lock_program(NEW.program_id);
  IF TG_OP='UPDATE' AND (NEW.program_id<>OLD.program_id OR NEW.type<>OLD.type
    OR NEW.input_version_id<>OLD.input_version_id
    OR NEW.input_graph_state_id IS DISTINCT FROM OLD.input_graph_state_id
    OR (OLD.checked_version_id IS NOT NULL AND NEW.checked_version_id IS DISTINCT FROM OLD.checked_version_id))
  THEN RAISE EXCEPTION 'Вход операции неизменяем; создайте новую операцию' USING ERRCODE='23514'; END IF;
  PERFORM graph_require_sealed(NEW.program_id,NEW.input_graph_state_id);
  PERFORM graph_require_sealed(NEW.program_id,NEW.output_graph_state_id);
  IF NEW.output_version_id IS NOT NULL AND (NEW.output_graph_state_id IS NULL OR NOT EXISTS(
    SELECT 1 FROM program_versions WHERE id=NEW.output_version_id AND program_id=NEW.program_id AND graph_state_id=NEW.output_graph_state_id))
  THEN RAISE EXCEPTION 'Выход операции не соответствует версии' USING ERRCODE='23514'; END IF;
  IF NEW.type='generate' AND NEW.input_graph_state_id IS NULL
  THEN RAISE EXCEPTION 'Для генерации нужен закреплённый граф' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION graph_guard_package() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE j processing_jobs;
BEGIN
  PERFORM graph_lock_program(NEW.program_id);
  PERFORM graph_require_sealed(NEW.program_id,NEW.graph_state_id);
  SELECT * INTO j FROM processing_jobs WHERE id=NEW.job_id AND program_id=NEW.program_id;
  IF NOT FOUND OR j.type<>'generate' OR j.input_graph_state_id IS DISTINCT FROM NEW.graph_state_id
  THEN RAISE EXCEPTION 'ZIP не соответствует входу генерации' USING ERRCODE='23514'; END IF;
  IF NEW.version_id IS NOT NULL AND (NEW.version_id<>j.input_version_id OR NOT EXISTS(
    SELECT 1 FROM program_versions WHERE id=NEW.version_id AND program_id=NEW.program_id AND graph_state_id=NEW.graph_state_id))
  THEN RAISE EXCEPTION 'ZIP не соответствует версии' USING ERRCODE='23514'; END IF;
  -- Повтор импорта может уже иметь другой результат: проверяется производитель закреплённого состояния.
  IF NEW.import_job_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM graph_states s JOIN processing_jobs i ON i.id=s.producing_job_id
    WHERE s.id=NEW.graph_state_id AND s.program_id=NEW.program_id AND i.type='import' AND i.id=NEW.import_job_id)
  THEN RAISE EXCEPTION 'ZIP не соответствует выбранному импорту' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['program_versions','entity_revisions','relation_revisions',
    'source_documents','document_artifacts','source_refs','source_links','revision_sources','change_log','generated_packages'] LOOP
    EXECUTE format('CREATE TRIGGER immutable_row BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION graph_reject_mutation()',t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['graph_entities','graph_relations','graph_documents','graph_sources','graph_source_links'] LOOP
    EXECUTE format('CREATE TRIGGER building_only BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION graph_guard_membership()',t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['program_versions','graph_states','entity_revisions','relation_revisions','source_documents',
    'document_artifacts','source_refs','source_links','revision_sources','change_log','generated_packages',
    'graph_entities','graph_relations','graph_documents','graph_sources','graph_source_links'] LOOP
    EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION graph_reject_mutation()',t);
  END LOOP;
END $$;
CREATE TRIGGER guard_state BEFORE INSERT OR UPDATE OR DELETE ON graph_states FOR EACH ROW EXECUTE FUNCTION graph_guard_state();
CREATE TRIGGER guard_version BEFORE INSERT ON program_versions FOR EACH ROW EXECUTE FUNCTION graph_guard_version();
CREATE TRIGGER guard_revision_sources BEFORE INSERT ON revision_sources FOR EACH ROW EXECUTE FUNCTION graph_guard_revision_sources();
CREATE TRIGGER guard_job BEFORE INSERT OR UPDATE ON processing_jobs FOR EACH ROW EXECUTE FUNCTION graph_guard_job();
CREATE TRIGGER guard_package BEFORE INSERT ON generated_packages FOR EACH ROW EXECUTE FUNCTION graph_guard_package();
