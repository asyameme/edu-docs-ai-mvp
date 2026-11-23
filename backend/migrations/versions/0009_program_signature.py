"""Текущая подпись хранится в программе независимо от истории действий."""

from alembic import op

revision = "0009_program_signature"
down_revision = "0008_pregraph_review"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE programs ADD COLUMN signature text")
    op.execute(
        "ALTER TABLE programs ADD CONSTRAINT programs_signature_length "
        "CHECK (signature IS NULL OR char_length(signature) BETWEEN 1 AND 120)"
    )


def downgrade():
    op.execute("ALTER TABLE programs DROP COLUMN signature")
