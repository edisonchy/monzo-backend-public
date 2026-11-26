"""Private dashboard sessions and shared login rate limit."""

import sqlalchemy as sa
from alembic import op

revision = "d43f86ab1504"
down_revision = "c32e75fa0403"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "browser_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("credential_tag", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "browser_login_window",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("browser_login_window")
    op.drop_table("browser_sessions")
