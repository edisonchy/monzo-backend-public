"""Separate browser login allowances by trusted client identity."""

import sqlalchemy as sa
from alembic import op

revision = "ec21a40d9353"
down_revision = "db10f30c8242"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "browser_login_buckets",
        sa.Column("client_key", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index("ix_browser_login_buckets_started_at", "browser_login_buckets", ["started_at"])


def downgrade():
    op.drop_table("browser_login_buckets")
