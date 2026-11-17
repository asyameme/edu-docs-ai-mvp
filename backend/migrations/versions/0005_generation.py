"""Долговечное хранение ZIP, созданного для конкретной версии программы."""

from alembic import op

revision = "0005_generation"
down_revision = "0004_relational"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE generated_packages (
            job_id uuid PRIMARY KEY,
            program_id uuid NOT NULL,
            version_id uuid,
            import_job_id uuid,
            content bytea NOT NULL,
            size_bytes bigint NOT NULL CHECK (size_bytes > 0),
            sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (program_id, job_id) REFERENCES processing_jobs(program_id, id),
            FOREIGN KEY (program_id, version_id) REFERENCES program_versions(program_id, id),
            FOREIGN KEY (program_id, import_job_id) REFERENCES processing_jobs(program_id, id),
            CHECK ((version_id IS NULL) <> (import_job_id IS NULL)),
            CHECK (octet_length(content) = size_bytes),
            CHECK (encode(sha256(content), 'hex') = sha256)
        );
        CREATE INDEX generated_packages_version_idx
            ON generated_packages(program_id, version_id);
        CREATE TRIGGER immutable_generated_packages
            BEFORE UPDATE OR DELETE ON generated_packages
            FOR EACH ROW EXECUTE FUNCTION reject_original_mutation();
    """)


def downgrade():
    op.execute("DROP TABLE generated_packages")
