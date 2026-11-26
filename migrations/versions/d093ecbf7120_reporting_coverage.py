"""Evidence of completed, accessible sync windows; no inferred legacy coverage."""

import sqlalchemy as sa
from alembic import op

revision = "d093ecbf7120"
down_revision = "c982dbaf6019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reporting_coverage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(128), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "start_at", "end_at", "source"),
    )
    op.create_index("ix_reporting_coverage_account_id", "reporting_coverage", ["account_id"])
    op.execute("""
        CREATE CONSTRAINT TRIGGER dashboard_coverage_change
        AFTER INSERT OR UPDATE OR DELETE ON reporting_coverage
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION advance_dashboard_revision()
    """)


def downgrade():
    op.drop_table("reporting_coverage")
