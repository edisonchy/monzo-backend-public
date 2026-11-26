"""Persistent, direction-scoped payment grouping without bank merchant IDs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fa54d730c686"
down_revision = "fe43c62fb575"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "transactions",
        sa.Column(
            "payment_group_identity",
            sa.String(128),
            sa.ForeignKey(
                "merchant_identities.merchant_id", name="fk_transactions_payment_group_identity"
            ),
        ),
    )
    op.create_table(
        "payment_group_matchers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(128), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column(
            "identity_id",
            sa.String(128),
            sa.ForeignKey("merchant_identities.merchant_id"),
            nullable=False,
        ),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.execute("""
        REVOKE ALL ON payment_group_matchers FROM PUBLIC;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monzo_mcp') THEN
                REVOKE ALL ON payment_group_matchers FROM monzo_mcp;
            END IF;
        END $$;
    """)


def downgrade():
    op.drop_table("payment_group_matchers")
    op.drop_column("transactions", "payment_group_identity")
