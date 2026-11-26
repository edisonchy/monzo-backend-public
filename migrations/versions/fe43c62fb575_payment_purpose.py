"""Owner-confirmed payment purposes and bank-identity rules."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fe43c62fb575"
down_revision = "fd32b51ea464"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("transactions", sa.Column("counterparty_key", sa.String(64)))
    op.add_column("transactions", sa.Column("purpose_confirmation", postgresql.JSONB()))
    op.create_table(
        "payment_purpose_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(128), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("counterparty_key", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "counterparty_key", "direction"),
    )

    op.execute("""
        REVOKE ALL ON payment_purpose_rules FROM PUBLIC;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monzo_mcp') THEN
                REVOKE ALL ON payment_purpose_rules FROM monzo_mcp;
            END IF;
        END $$;
    """)


def downgrade():
    op.drop_table("payment_purpose_rules")
    op.drop_column("transactions", "purpose_confirmation")
    op.drop_column("transactions", "counterparty_key")
