"""Persist merchant groups, aliases and group-wide future category rules."""

import re
import unicodedata
import uuid

import sqlalchemy as sa
from alembic import op

revision = "c982dbaf6019"
down_revision = "b871caef5908"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "merchant_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("merged_into", sa.String(36), sa.ForeignKey("merchant_groups.id")),
    )
    op.create_table(
        "merchant_identities",
        sa.Column("merchant_id", sa.String(128), primary_key=True),
        sa.Column("group_id", sa.String(36), sa.ForeignKey("merchant_groups.id"), nullable=False),
    )
    op.create_index("ix_merchant_identities_group_id", "merchant_identities", ["group_id"])
    op.create_table(
        "merchant_aliases",
        sa.Column("name", sa.String(500), primary_key=True),
        sa.Column("group_id", sa.String(36), sa.ForeignKey("merchant_groups.id"), nullable=False),
    )
    op.create_index("ix_merchant_aliases_group_id", "merchant_aliases", ["group_id"])
    op.alter_column("category_rules", "merchant_id", nullable=True)
    op.add_column(
        "category_rules",
        sa.Column("group_id", sa.String(36), sa.ForeignKey("merchant_groups.id")),
    )
    op.create_unique_constraint("uq_category_rules_group_id", "category_rules", ["group_id"])
    db = op.get_bind()
    aliases, members = {}, {}
    # Carry forward the approved spelling/branch aliases once; runtime uses stored data.
    approved = {
        "tesco express": "tesco",
        "tesco extra": "tesco",
        "sainsburys": "sainsbury's",
        "mcdonalds": "mcdonald's",
        "costa": "costa coffee",
    }
    for mid, name in db.execute(
        sa.text(
            "SELECT merchant_id, merchant_name FROM transactions WHERE merchant_id IS NOT NULL AND merchant_id <> '' ORDER BY created_at, id"
        )
    ):
        norm = (
            re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name or ""))
            .strip()
            .casefold()
            .replace("’", "'")
        )
        if norm in {"", "unknown", "unnamed payment", "unknown merchant", "payment"}:
            norm = ""
        canonical = approved.get(norm, norm)
        if mid in members:
            continue
        group = aliases.get(canonical) if canonical else None
        if not group:
            group = str(uuid.uuid4())
            db.execute(
                sa.text("INSERT INTO merchant_groups VALUES (:id, :name, 1, NULL)"),
                {"id": group, "name": (name or "Unnamed merchant").strip()},
            )
        members[mid] = group
        db.execute(
            sa.text("INSERT INTO merchant_identities VALUES (:mid, :gid)"),
            {"mid": mid, "gid": group},
        )
        for key in {norm, canonical} - {""}:
            if key not in aliases:
                aliases[key] = group
                db.execute(
                    sa.text("INSERT INTO merchant_aliases VALUES (:name, :gid)"),
                    {"name": key, "gid": group},
                )
    # Promote only rules already agreed across every known identity; leave exceptions intact.
    for gid, category, effective in db.execute(
        sa.text("""
        SELECT i.group_id, min(r.category), max(r.effective_from)
        FROM merchant_identities i LEFT JOIN category_rules r ON r.merchant_id=i.merchant_id
        GROUP BY i.group_id HAVING count(r.id)=count(*) AND count(DISTINCT r.category)=1
    """)
    ):
        db.execute(
            sa.text(
                "INSERT INTO category_rules (id,group_id,category,effective_from) VALUES (:id,:gid,:category,:effective)"
            ),
            {"id": str(uuid.uuid4()), "gid": gid, "category": category, "effective": effective},
        )
    for table in ("merchant_groups", "merchant_identities", "merchant_aliases"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER dashboard_group_change AFTER INSERT OR UPDATE OR DELETE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION advance_dashboard_revision()"
        )


def downgrade():
    op.execute("DELETE FROM category_rules WHERE group_id IS NOT NULL")
    op.drop_column("category_rules", "group_id")
    op.alter_column("category_rules", "merchant_id", nullable=False)
    op.drop_table("merchant_aliases")
    op.drop_table("merchant_identities")
    op.drop_table("merchant_groups")
