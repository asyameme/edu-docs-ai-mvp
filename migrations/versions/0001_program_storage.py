"""Начальное хранилище приложения; остальные таблицы и базы Temporal не меняются."""

from alembic import op

revision = "0001_program_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE programs (
            id uuid PRIMARY KEY, name text NOT NULL CHECK (length(trim(name)) > 0),
            current_version_id uuid,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE program_versions (
            id uuid PRIMARY KEY,
            program_id uuid NOT NULL REFERENCES programs(id),
            number bigint NOT NULL CHECK (number > 0),
            parent_version_id uuid,
            restored_from_version_id uuid,
            snapshot jsonb NOT NULL CHECK (
                jsonb_typeof(snapshot) = 'object' AND snapshot ? 'schema_version'
                AND snapshot->>'schema_version' = '1'),
            origin text NOT NULL CHECK (origin IN ('initial','save','restore')),
            operation_key text,
            request_hash text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (program_id, id), UNIQUE (program_id, number),
            UNIQUE (program_id, operation_key),
            CHECK ((operation_key IS NULL) = (request_hash IS NULL)),
            FOREIGN KEY (program_id, parent_version_id) REFERENCES program_versions(program_id, id),
            FOREIGN KEY (program_id, restored_from_version_id)
                REFERENCES program_versions(program_id, id)
        );
        ALTER TABLE programs ADD CONSTRAINT programs_current_version_fk
            FOREIGN KEY (id, current_version_id) REFERENCES program_versions(program_id, id)
            DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE source_documents (
            id uuid PRIMARY KEY, program_id uuid NOT NULL REFERENCES programs(id),
            original_name text NOT NULL,
            format text NOT NULL CHECK (format IN ('pdf','docx','xlsx')),
            media_type text NOT NULL, size_bytes bigint NOT NULL CHECK (size_bytes > 0),
            sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
            storage_key text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (program_id, id)
        );
        CREATE TABLE version_documents (
            program_id uuid NOT NULL, version_id uuid NOT NULL, document_id uuid NOT NULL,
            PRIMARY KEY (version_id, document_id),
            FOREIGN KEY (program_id, version_id) REFERENCES program_versions(program_id, id),
            FOREIGN KEY (program_id, document_id) REFERENCES source_documents(program_id, id)
        );
        CREATE INDEX programs_created_idx ON programs (created_at DESC, id);
        CREATE INDEX documents_program_created_idx
            ON source_documents (program_id, created_at DESC, id);
        CREATE INDEX version_documents_document_idx ON version_documents (program_id, document_id);
        CREATE INDEX versions_parent_idx ON program_versions (program_id, parent_version_id);
        CREATE INDEX versions_restored_idx
            ON program_versions (program_id, restored_from_version_id);
        CREATE INDEX programs_current_idx ON programs (id, current_version_id);
        CREATE FUNCTION reject_original_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Historical records are immutable' USING ERRCODE='23514'; END;
        $$;
        CREATE TRIGGER immutable_versions BEFORE UPDATE OR DELETE ON program_versions
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
        CREATE TRIGGER immutable_documents BEFORE UPDATE OR DELETE ON source_documents
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
        CREATE TRIGGER immutable_version_documents BEFORE UPDATE OR DELETE ON version_documents
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
    """)


def downgrade():
    op.execute("""
        DROP TABLE version_documents;
        DROP TABLE source_documents;
        ALTER TABLE programs DROP CONSTRAINT programs_current_version_fk;
        DROP TABLE program_versions;
        DROP TABLE programs;
        DROP FUNCTION reject_original_mutation();
    """)
