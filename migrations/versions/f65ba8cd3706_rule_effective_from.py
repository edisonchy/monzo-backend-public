"""Bound new merchant rules to future-dated payments; preserve legacy rule semantics."""

import sqlalchemy as sa
from alembic import op

revision = "f65ba8cd3706"
down_revision = "e54a97bc2605"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "category_rules", sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("category_rules", "effective_from")
