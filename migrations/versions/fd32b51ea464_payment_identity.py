"""Bank-supplied payment display identities, independent of classification."""

import sqlalchemy as sa
from alembic import op

revision = "fd32b51ea464"
down_revision = "ec21a40d9353"
branch_labels = None
depends_on = None


def revision_trigger(extra=""):
    ignored = "'updated_at','reporting_checked_at','pending_check_after'" + extra
    op.execute("DROP TRIGGER dashboard_update ON transactions")
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER dashboard_update AFTER UPDATE ON transactions
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (
          (to_jsonb(OLD) - ARRAY[{ignored}]) IS DISTINCT FROM
          (to_jsonb(NEW) - ARRAY[{ignored}])
        ) EXECUTE FUNCTION advance_dashboard_revision()
    """)


def upgrade():
    for name, kind in (
        ("counterparty_name", sa.String(500)),
        ("payment_name", sa.String(2000)),
        ("payment_name_source", sa.String(40)),
        ("identity_checked_at", sa.DateTime(timezone=True)),
        ("identity_retry_after", sa.DateTime(timezone=True)),
    ):
        op.add_column("transactions", sa.Column(name, kind))
    op.add_column(
        "transactions",
        sa.Column("identity_attempts", sa.Integer(), nullable=False, server_default="0"),
    )

    revision_trigger(", 'identity_checked_at', 'identity_retry_after', 'identity_attempts'")


def downgrade():
    revision_trigger()
    for name in (
        "counterparty_name",
        "payment_name",
        "payment_name_source",
        "identity_checked_at",
        "identity_retry_after",
        "identity_attempts",
    ):
        op.drop_column("transactions", name)
