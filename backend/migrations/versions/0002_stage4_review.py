"""Источники полей, результаты проверок и неизменяемые Markdown-артефакты."""

from alembic import op

revision = "0002_stage4_review"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE program_versions DROP CONSTRAINT program_versions_snapshot_check;
        ALTER TABLE program_versions ADD CONSTRAINT program_versions_snapshot_check CHECK (
            jsonb_typeof(snapshot)='object' AND snapshot->>'schema_version' IS NOT NULL
            AND snapshot->>'schema_version' IN ('1','2'));
        CREATE TABLE document_artifacts (
            id uuid PRIMARY KEY, program_id uuid NOT NULL, document_id uuid NOT NULL,
            producing_job_id uuid NOT NULL,
            kind text NOT NULL DEFAULT 'markdown' CHECK (kind='markdown'),
            parser_revision text NOT NULL,
            parser_config_hash text NOT NULL CHECK (parser_config_hash ~ '^[a-f0-9]{64}$'),
            storage_key text NOT NULL UNIQUE,
            sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
            size_bytes bigint NOT NULL CHECK (size_bytes>=0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(document_id,id), UNIQUE(producing_job_id,document_id,kind),
            FOREIGN KEY(program_id,document_id) REFERENCES source_documents(program_id,id),
            FOREIGN KEY(program_id,producing_job_id) REFERENCES processing_jobs(program_id,id)
        );
        CREATE INDEX artifacts_document_idx ON document_artifacts(program_id,document_id);
        CREATE INDEX artifacts_job_idx ON document_artifacts(program_id,producing_job_id);
        CREATE TABLE source_refs (
            id uuid PRIMARY KEY, version_id uuid NOT NULL, document_id uuid NOT NULL,
            artifact_id uuid, section text,
            locator text NOT NULL CHECK(length(trim(locator))>0), artifact_locator text,
            UNIQUE(version_id,id),
            FOREIGN KEY(version_id,document_id)
                REFERENCES version_documents(version_id,document_id),
            FOREIGN KEY(document_id,artifact_id) REFERENCES document_artifacts(document_id,id),
            CHECK ((artifact_id IS NULL AND artifact_locator IS NULL) OR
                (artifact_id IS NOT NULL AND artifact_locator IS NOT NULL
                 AND length(trim(artifact_locator))>0))
        );
        CREATE INDEX refs_document_idx ON source_refs(version_id,document_id);
        CREATE INDEX refs_artifact_idx ON source_refs(document_id,artifact_id);
        CREATE TABLE source_links (
            id uuid PRIMARY KEY, version_id uuid NOT NULL,
            target_kind text NOT NULL CHECK(target_kind IN ('entity','relation')),
            target_id uuid NOT NULL, field_path text NOT NULL CHECK(length(trim(field_path))>0),
            source_ref_id uuid NOT NULL,
            observed_value jsonb NOT NULL CHECK(jsonb_typeof(observed_value)='object'),
            UNIQUE(version_id,target_kind,target_id,field_path,source_ref_id),
            FOREIGN KEY(version_id,source_ref_id) REFERENCES source_refs(version_id,id)
        );
        CREATE INDEX links_source_idx ON source_links(version_id,source_ref_id);
        CREATE TABLE validation_issues (
            id uuid PRIMARY KEY, program_id uuid NOT NULL, job_id uuid NOT NULL,
            version_id uuid NOT NULL, rule_code text NOT NULL, rule_revision text NOT NULL,
            type text NOT NULL, severity text NOT NULL CHECK(severity IN ('warning','blocking')),
            status text NOT NULL CHECK(status IN ('open','needs_review')),
            details jsonb NOT NULL CHECK(jsonb_typeof(details)='object'),
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY(program_id,job_id) REFERENCES processing_jobs(program_id,id),
            FOREIGN KEY(program_id,version_id) REFERENCES program_versions(program_id,id)
        );
        CREATE INDEX issues_job_idx ON validation_issues(program_id,job_id);
        CREATE INDEX issues_version_idx ON validation_issues(program_id,version_id);
        CREATE INDEX issues_rule_idx ON validation_issues(version_id,rule_code);
        CREATE TRIGGER immutable_artifacts BEFORE UPDATE OR DELETE ON document_artifacts
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
        CREATE TRIGGER immutable_refs BEFORE UPDATE OR DELETE ON source_refs
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
        CREATE TRIGGER immutable_links BEFORE UPDATE OR DELETE ON source_links
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
        CREATE TRIGGER immutable_issues BEFORE UPDATE OR DELETE ON validation_issues
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM program_versions WHERE snapshot->>'schema_version'='2')
                OR EXISTS(SELECT 1 FROM document_artifacts)
                OR EXISTS(SELECT 1 FROM source_refs)
                OR EXISTS(SELECT 1 FROM source_links)
                OR EXISTS(SELECT 1 FROM validation_issues)
                OR EXISTS(SELECT 1 FROM processing_jobs WHERE type='validate') THEN
                RAISE EXCEPTION 'Cannot discard stage4 data; restore a verified backup instead';
            END IF;
        END $$;
        DROP TABLE validation_issues;
        DROP TABLE source_links;
        DROP TABLE source_refs;
        DROP TABLE document_artifacts;
        ALTER TABLE program_versions DROP CONSTRAINT program_versions_snapshot_check;
        ALTER TABLE program_versions ADD CONSTRAINT program_versions_snapshot_check CHECK (
            jsonb_typeof(snapshot)='object' AND snapshot ? 'schema_version'
            AND snapshot->>'schema_version'='1');
    """)
