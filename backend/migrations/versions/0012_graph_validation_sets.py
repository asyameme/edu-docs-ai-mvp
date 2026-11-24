"""Проверять принадлежность источников и свидетельств операциями над наборами."""

from pathlib import Path

from alembic import op

revision = "0012_graph_validation_sets"
down_revision = "0011_merge_graph_signature"
branch_labels = None
depends_on = None


def upgrade():
    folder = Path(__file__).parents[1] / "sql"
    op.execute((folder / "graph_validate_contents_sets.sql").read_text(encoding="utf-8"))


def downgrade():
    source = (Path(__file__).parents[1] / "sql/revision_evidence.sql").read_text(encoding="utf-8")
    start = source.index("CREATE FUNCTION graph_validate_contents(")
    end = source.index("CREATE FUNCTION graph_guard_contents(", start)
    op.execute(source[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
