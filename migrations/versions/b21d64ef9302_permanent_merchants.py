"""Keep existing and future merchant decisions without automatic expiry."""

import sqlalchemy as sa
from alembic import op

revision = "b21d64ef9302"
down_revision = "a19c53de8201"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("merchant_decisions", "expires_at", nullable=True)
    op.execute("UPDATE merchant_decisions SET expires_at = NULL")


def downgrade():
    op.execute(
        "UPDATE merchant_decisions SET expires_at = CURRENT_TIMESTAMP + INTERVAL '30 days' WHERE expires_at IS NULL"
    )
    op.alter_column(
        "merchant_decisions", "expires_at", existing_type=sa.DateTime(timezone=True), nullable=False
    )
