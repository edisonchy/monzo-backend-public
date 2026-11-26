"""Permanent native web research, including inconclusive results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c32e75fa0403"
down_revision = "b21d64ef9302"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "merchant_research",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("merchant_name", sa.String(160), nullable=False),
        sa.Column("finding", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usage", postgresql.JSONB(), nullable=False),
    )


def downgrade():
    op.drop_table("merchant_research")
