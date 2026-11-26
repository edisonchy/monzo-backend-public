"""Persist bank reconnection outcomes and webhook account evidence."""

import sqlalchemy as sa
from alembic import op

revision = "b98ed1fa6020"
down_revision = "a87dc0ef5919"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "oauth_states",
        sa.Column("outcome", sa.String(30), nullable=False, server_default="pending"),
    )
    op.execute("UPDATE oauth_states SET outcome='legacy' WHERE used")
    op.add_column("webhook_events", sa.Column("account_id", sa.String(128)))
    op.execute(
        "UPDATE webhook_events e SET account_id=t.account_id FROM transactions t WHERE e.transaction_id=t.id"
    )


def downgrade():
    op.drop_column("webhook_events", "account_id")
    op.drop_column("oauth_states", "outcome")
