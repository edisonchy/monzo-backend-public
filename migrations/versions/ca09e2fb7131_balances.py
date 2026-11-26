"""Store verified balance observations and daily successful snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ca09e2fb7131"
down_revision = "b98ed1fa6020"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("balance_observations", "balance_snapshots"):
        snapshot = name == "balance_snapshots"
        op.create_table(
            name,
            sa.Column("account_id", sa.String(128), sa.ForeignKey("accounts.id"), primary_key=True),
            sa.Column("source", sa.String(20), primary_key=True),
            *([sa.Column("day", sa.Date(), primary_key=True)] if snapshot else []),
            sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
            *([] if snapshot else [sa.Column("outcome", sa.String(20), nullable=False)]),
            sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=not snapshot),
            sa.Column("provider_observed_at", sa.DateTime(timezone=True)),
            sa.Column("data", postgresql.JSONB(), nullable=not snapshot),
        )
    op.execute("""CREATE CONSTRAINT TRIGGER dashboard_balance_change
        AFTER INSERT OR UPDATE OR DELETE ON balance_observations
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION advance_dashboard_revision()""")


def downgrade():
    op.drop_table("balance_snapshots")
    op.drop_table("balance_observations")
