"""Minimal bank evidence for Combined spending exclusions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e54a97bc2605"
down_revision = "d43f86ab1504"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("transactions", sa.Column("reporting_facts", postgresql.JSONB(), nullable=True))
    op.add_column(
        "transactions", sa.Column("reporting_checked_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("transactions", "reporting_checked_at")
    op.drop_column("transactions", "reporting_facts")
