"""Сессии и сообщения чата; состояние исполнения остаётся в Temporal."""

from alembic import op

revision = "0007_chat"
down_revision = "0006_versioned_graph"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE chat_sessions (
            id uuid PRIMARY KEY,
            program_id uuid NOT NULL REFERENCES programs(id),
            version_id uuid NOT NULL,
            title text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (program_id, version_id)
                REFERENCES program_versions(program_id, id)
        );
        CREATE INDEX chat_sessions_program_updated_idx
            ON chat_sessions(program_id, updated_at DESC, id);
        CREATE TABLE chat_turns (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES chat_sessions(id),
            operation_key text NOT NULL,
            request_hash text NOT NULL CHECK (length(request_hash)=64),
            user_text text NOT NULL CHECK (length(user_text) BETWEEN 1 AND 12000),
            workflow_id text NOT NULL UNIQUE,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','completed','failed')),
            result jsonb CHECK (result IS NULL OR jsonb_typeof(result)='object'),
            conversation bytea,
            created_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            UNIQUE(session_id, operation_key)
        );
        CREATE INDEX chat_turns_session_created_idx
            ON chat_turns(session_id, created_at, id);
        """
    )


def downgrade():
    op.execute("DROP TABLE chat_turns; DROP TABLE chat_sessions")
