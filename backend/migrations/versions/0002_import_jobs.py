"""Долговечные задачи импорта и результаты разбора отдельных оригиналов."""

from alembic import op

revision = "0002_import_jobs"
down_revision = "0001_program_storage"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE import_jobs (
            id uuid PRIMARY KEY,
            program_id uuid NOT NULL REFERENCES programs(id),
            fingerprint text NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{64}$'),
            attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','processing','completed','partial','failed')),
            progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
            workflow_started boolean NOT NULL DEFAULT false,
            snapshot jsonb,
            issues jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(issues)='array'),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (program_id, id),
            UNIQUE (program_id, fingerprint)
        );
        CREATE TABLE import_requests (
            program_id uuid NOT NULL,
            operation_key text NOT NULL,
            job_id uuid NOT NULL,
            PRIMARY KEY (program_id, operation_key),
            FOREIGN KEY (program_id, job_id) REFERENCES import_jobs(program_id, id)
        );
        CREATE INDEX import_requests_job_idx ON import_requests (program_id, job_id);
        CREATE TABLE import_files (
            job_id uuid NOT NULL,
            program_id uuid NOT NULL,
            document_id uuid NOT NULL,
            member_name text NOT NULL,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','processing','completed','failed')),
            output jsonb,
            error_code text,
            error_message text,
            PRIMARY KEY (job_id, document_id),
            FOREIGN KEY (program_id, job_id) REFERENCES import_jobs(program_id, id),
            FOREIGN KEY (program_id, document_id) REFERENCES source_documents(program_id, id)
        );
        CREATE INDEX import_jobs_program_created_idx
            ON import_jobs (program_id, created_at DESC, id);
        CREATE INDEX import_jobs_dispatch_idx ON import_jobs (created_at, id)
            WHERE NOT workflow_started AND status='pending';
        CREATE INDEX import_files_document_idx ON import_files (program_id, document_id);
        CREATE INDEX documents_program_hash_idx ON source_documents (program_id, sha256);
    """)


def downgrade():
    op.execute("""
        DROP TABLE import_files;
        DROP TABLE import_requests;
        DROP TABLE import_jobs;
        DROP INDEX documents_program_hash_idx;
    """)
