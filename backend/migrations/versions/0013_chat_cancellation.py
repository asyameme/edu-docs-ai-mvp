from alembic import op

revision = "0013_chat_cancellation"
down_revision = "0012_graph_validation_sets"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("chat_turns_status_check", "chat_turns", type_="check")
    op.create_check_constraint(
        "chat_turns_status_check", "chat_turns",
        "status IN ('pending','cancelling','cancelled','completed','failed')",
    )


def downgrade():
    op.execute(
        "UPDATE chat_turns SET status='failed',completed_at=COALESCE(completed_at,now()) "
        "WHERE status IN ('cancelling','cancelled')"
    )
    op.drop_constraint("chat_turns_status_check", "chat_turns", type_="check")
    op.create_check_constraint(
        "chat_turns_status_check", "chat_turns",
        "status IN ('pending','completed','failed')",
    )
