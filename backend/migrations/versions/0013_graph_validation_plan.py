"""Стабильный план проверки больших составов графа без изменения ограничений."""

from alembic import op

revision = "0013_graph_validation_plan"
down_revision = "0012_graph_validation_sets"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER FUNCTION graph_validate_contents(uuid) SET jit TO off")
    op.execute("ALTER FUNCTION graph_validate_contents(uuid) SET enable_nestloop TO off")


def downgrade():
    op.execute("ALTER FUNCTION graph_validate_contents(uuid) RESET jit")
    op.execute("ALTER FUNCTION graph_validate_contents(uuid) RESET enable_nestloop")
