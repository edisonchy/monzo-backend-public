"""Cache validated merchant decisions independently of transaction assignments."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a19c53de8201"
down_revision = "e78b11ffec3d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "merchant_decisions",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("decision", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("merchant_decisions")
