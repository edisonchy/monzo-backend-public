"""User-defined dashboard categories share the existing payment category field."""

import unicodedata
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert

from monzo_backend.category_policy import CATEGORIES
from monzo_backend.models import Audit, BudgetVersion, CustomCategory, Rule, Transaction, now


class NewCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=50)
    colour: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


def keys(s):
    return [*CATEGORIES, *s.scalars(select(CustomCategory.key).order_by(CustomCategory.identity))]


def known(s, value):
    return value in CATEGORIES or (
        value.startswith("custom:") and s.get(CustomCategory, value) is not None
    )


def create(s, body):
    name = " ".join(unicodedata.normalize("NFKC", body.name).split())
    identity = name.casefold()
    reserved = {key.replace("_", " ") for key in (*CATEGORIES, "uncategorised")}
    if not name or len(name) > 50 or any(unicodedata.category(c) == "Cc" for c in name):
        raise HTTPException(422, "Enter a category name of up to 50 characters")
    if identity.replace("_", " ") in reserved | {"bills & subscriptions"}:
        raise HTTPException(409, "That category already exists")
    key, colour = "custom:" + name, body.colour.lower()
    inserted = s.scalar(
        insert(CustomCategory)
        .values(key=key, identity=identity, colour=colour)
        .on_conflict_do_nothing()
        .returning(CustomCategory.key)
    )
    if not inserted:
        existing = s.scalar(select(CustomCategory).where(CustomCategory.identity == identity))
        if existing and existing.colour == colour:
            return {"key": existing.key, "colour": colour}
        raise HTTPException(409, "A category with that name already exists")
    return {"key": key, "colour": colour}


class CategoryRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    replacement: str | None = None
    revision: str


def usage(s, category):
    row_version = s.scalar(
        select(literal_column("xmin::text"))
        .select_from(CustomCategory)
        .where(CustomCategory.key == category)
    )
    if row_version is None:
        raise HTTPException(404, "This custom category no longer exists")
    payments, rules = [
        s.scalar(select(func.count()).select_from(table).where(table.category == category))
        for table in (Transaction, Rule)
    ]
    latest = (
        select(BudgetVersion.amount_minor)
        .where(
            BudgetVersion.category == category,
            BudgetVersion.month
            >= now().astimezone(ZoneInfo("Europe/London")).date().replace(day=1),
        )
        .distinct(BudgetVersion.account_scope, BudgetVersion.month)
        .order_by(BudgetVersion.account_scope, BudgetVersion.month, BudgetVersion.version.desc())
        .subquery()
    )
    budgets = s.scalar(
        select(func.count()).select_from(latest).where(latest.c.amount_minor.is_not(None))
    )
    return {
        "payments": payments,
        "rules": rules,
        "budgets": budgets,
        "revision": f"{row_version}:{payments}:{rules}:{budgets}",
    }


def remove(s, body):
    s.execute(text("SELECT pg_advisory_xact_lock(741820, 1)"))
    info = usage(s, body.category)
    if info["revision"] != body.revision:
        raise HTTPException(
            409, "Category usage changed. Check the updated counts before deleting."
        )
    if info["budgets"]:
        raise HTTPException(
            409, "Clear the current or upcoming budget targets for this category first."
        )
    dest = body.replacement
    if dest == body.category or (dest is not None and not known(s, dest)):
        raise HTTPException(422, "Choose a different category")
    if (info["payments"] or info["rules"]) and not dest:
        raise HTTPException(409, "Choose where to move the payments or merchant rules first")
    if dest:
        s.execute(
            update(Transaction)
            .where(Transaction.category == body.category)
            .values(category=dest, version=Transaction.version + 1, updated_at=now())
        )
        s.execute(update(Rule).where(Rule.category == body.category).values(category=dest))
    s.delete(s.get(CustomCategory, body.category))
    s.add(
        Audit(
            actor="owner",
            action="custom_category_deleted",
            detail={"category": body.category, "replacement": dest, **info},
        )
    )
    return {"removed": body.category, "replacement": dest}
