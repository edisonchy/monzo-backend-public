"""Append-only monthly budget targets; no real targets seeded."""

import sqlalchemy as sa
from alembic import op

revision = "db10f30c8242"
down_revision = "ca09e2fb7131"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "budget_versions",
        sa.Column(
            "connection_id", sa.Integer(), sa.ForeignKey("bank_connections.id"), primary_key=True
        ),
        sa.Column("account_scope", sa.String(128), primary_key=True),
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("category", sa.String(100), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("currency = 'GBP'", name="budget_currency"),
        sa.CheckConstraint("version > 0", name="budget_version_positive"),
        sa.CheckConstraint(
            "amount_minor IS NULL OR amount_minor BETWEEN 0 AND 1000000000000", name="budget_amount"
        ),
        sa.CheckConstraint("EXTRACT(DAY FROM month) = 1", name="budget_month_start"),
    )
    op.execute("""CREATE FUNCTION protect_budget_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Budget history is append-only'; END $$""")
    op.execute("""CREATE TRIGGER budget_history_immutable BEFORE UPDATE OR DELETE ON budget_versions
        FOR EACH ROW EXECUTE FUNCTION protect_budget_history()""")
    op.execute("""CREATE CONSTRAINT TRIGGER dashboard_budget_change
        AFTER INSERT ON budget_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION advance_dashboard_revision()""")


def downgrade():
    op.drop_table("budget_versions")
    op.execute("DROP FUNCTION protect_budget_history()")
