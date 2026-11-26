"""Shared financial predicates and integer aggregates for dashboard and MCP."""

from sqlalchemy import case, func, select

from monzo_backend.card_checks import CARD_CHECK
from monzo_backend.domain import CATEGORIES
from monzo_backend.models import Transaction

# The category-reference trigger validates custom keys on writes. Reports only need the payment row.
EFFECTIVE = case(
    (
        Transaction.category_status.in_(("assigned", "needs_review"))
        & (Transaction.category.in_(CATEGORIES) | Transaction.category.startswith("custom:")),
        Transaction.category,
    ),
    else_="uncategorised",
)


DEBIT = case((Transaction.amount < 0, -Transaction.amount), else_=0)
CREDIT = case((Transaction.amount > 0, Transaction.amount), else_=0)


def settled_conditions():
    return [Transaction.declined.is_(False), Transaction.settled_at.is_not(None), ~CARD_CHECK]


def money_totals(s, filters):
    return s.execute(
        select(
            func.coalesce(func.sum(DEBIT), 0),
            func.coalesce(func.sum(CREDIT), 0),
            func.count(),
        )
        .select_from(Transaction)
        .where(*filters)
    ).one()
