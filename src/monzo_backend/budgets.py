"""Owner-only local targets. Reads use stored ledger evidence; writes never touch the bank."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, text

from monzo_backend import custom_categories
from monzo_backend.card_checks import CARD_CHECK
from monzo_backend.category_policy import CATEGORIES
from monzo_backend.dashboard import TZ, bounds, conditions
from monzo_backend.db import session
from monzo_backend.models import (
    Account,
    Audit,
    BudgetVersion,
    DashboardRevision,
    ReportingCoverage,
    Transaction,
    now,
)
from monzo_backend.reporting import CREDIT, DEBIT, EFFECTIVE, settled_conditions
from monzo_backend.spending import ACCOUNT_KINDS, COMBINED, exclusions


class BudgetEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    period: str = Field(pattern=r"^(20\d{2}|2100)-(0[1-9]|1[0-2])$")
    account_id: str = Field(default=COMBINED, min_length=1, max_length=128)
    currency: Literal["GBP"] = "GBP"
    category: str = Field(default="", max_length=100)
    amount_minor: int | None = Field(ge=0, le=1000000000000)
    expected_version: int = Field(ge=0, le=2147483646)


def accounts(s, scope):
    ids = list(
        s.scalars(
            select(Account.id)
            .where(Account.connection_id == 1, Account.kind.in_(ACCOUNT_KINDS))
            .order_by(Account.id)
        )
    )
    if not ids or (scope != COMBINED and scope not in ids):
        raise HTTPException(404, "Choose a supported connected account")
    return ids if scope == COMBINED else [scope]


def key_filters(scope, month):
    return [
        BudgetVersion.connection_id == 1,
        BudgetVersion.account_scope == scope,
        BudgetVersion.month == month,
    ]


def save(edit):
    month = bounds(edit.period, "month")[0].date()
    with session() as s, s.begin():
        if edit.category not in ("", "uncategorised") and not custom_categories.known(
            s, edit.category
        ):
            raise HTTPException(422, "Choose a supported category")
        accounts(s, edit.account_id)
        # Include the absent-row case: SELECT FOR UPDATE alone cannot serialise first creation.
        lock = f"budget:1:{edit.account_id}:{month}:{edit.category}"
        s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock})
        if month < now().astimezone(TZ).date().replace(day=1):
            raise HTTPException(
                409, "Past months are read-only. Choose the current or a future month."
            )
        previous = s.scalar(
            select(BudgetVersion)
            .where(*key_filters(edit.account_id, month), BudgetVersion.category == edit.category)
            .order_by(BudgetVersion.version.desc())
            .limit(1)
        )
        version = previous.version if previous else 0
        if version != edit.expected_version:
            raise HTTPException(
                409, "This target changed. Reload the latest target before saving again."
            )
        if (previous.amount_minor if previous else None) == edit.amount_minor:
            return {"version": version, "changed": False}
        s.add(
            BudgetVersion(
                connection_id=1,
                account_scope=edit.account_id,
                month=month,
                category=edit.category,
                version=version + 1,
                currency="GBP",
                amount_minor=edit.amount_minor,
            )
        )
        s.add(
            Audit(
                actor="owner",
                action="budget_saved",
                detail={
                    "scope": edit.account_id,
                    "month": edit.period,
                    "category": edit.category,
                    "version": version + 1,
                    "amount_minor": edit.amount_minor,
                },
            )
        )
        return {"version": version + 1, "changed": True}


def coverage(s, ids, start, end, observed):
    until = min(end, observed)
    windows = {
        aid: list(intervals)
        for aid, intervals in s.execute(
            select(
                ReportingCoverage.account_id,
                func.range_agg(
                    func.tstzrange(ReportingCoverage.start_at, ReportingCoverage.end_at, "[)")
                ),
            )
            .where(ReportingCoverage.account_id.in_(ids))
            .group_by(ReportingCoverage.account_id)
        )
    }
    items = []
    for aid in ids:
        covered_until = (
            next(
                (min(r.upper, until) for r in windows.get(aid, []) if r.lower <= start < r.upper),
                None,
            )
            if until > start
            else None
        )
        items.append(
            {
                "account_id": aid,
                "covered_until": covered_until,
                "complete": covered_until is not None and covered_until >= until,
            }
        )
    return {
        "state": "not_started"
        if until <= start
        else "complete"
        if all(i["complete"] for i in items)
        else "incomplete",
        "required_until": until if until > start else None,
        "accounts": items,
    }


def report(period, account_id=COMBINED):
    with session() as s, s.begin():
        s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return read_report(s, period, account_id, categories=custom_categories.keys(s))


def read_report(s, period, account_id=COMBINED, *, observed=None, categories=CATEGORIES):
    """Use the caller's snapshot and privileges; shared with the MCP reporting role."""
    start, end, _ = bounds(period, "month")
    observed = observed or now()
    ids = accounts(s, account_id)
    revision = s.scalar(select(DashboardRevision.revision).where(DashboardRevision.id == 1)) or 0
    excluded = exclusions(s, ids) if account_id == COMBINED else {}
    base = conditions(
        {
            "account_id": account_id,
            "currency": "GBP",
            "start": start,
            "end": end,
            "category": None,
            "merchant": None,
        }
    ) + [Transaction.account_id.in_(ids)]
    included = [Transaction.id.not_in(excluded)]

    def totals(settled):
        filters = (
            settled_conditions()
            if settled
            else [
                Transaction.declined.is_(False),
                Transaction.settled_at.is_(None),
                ~CARD_CHECK,
            ]
        )
        return {
            category: {"spent_minor": int(debit), "credits_minor": int(credit)}
            for category, debit, credit in s.execute(
                select(
                    EFFECTIVE,
                    func.coalesce(func.sum(DEBIT), 0),
                    func.coalesce(func.sum(CREDIT), 0),
                )
                .where(*base, *included, *filters)
                .group_by(EFFECTIVE)
            )
        }

    settled, pending = totals(True), totals(False)
    history = {}
    for row in s.scalars(
        select(BudgetVersion)
        .where(*key_filters(account_id, start.date()))
        .order_by(BudgetVersion.category, BudgetVersion.version.desc())
    ):
        history.setdefault(row.category, []).append(
            {
                "version": row.version,
                "amount_minor": row.amount_minor,
                "created_at": row.created_at,
            }
        )

    def item(category):
        versions = history.get(category, [])
        target = versions[0]["amount_minor"] if versions else None

        def amount(source, field):
            return (
                sum(v[field] for v in source.values())
                if category == ""
                else source.get(category, {}).get(field, 0)
            )

        spent = amount(settled, "spent_minor")
        return {
            "category": category,
            "target_minor": target,
            "version": versions[0]["version"] if versions else 0,
            "spent_minor": spent,
            "remaining_minor": None if target is None else target - spent,
            "pending_minor": amount(pending, "spent_minor"),
            "credits_minor": amount(settled, "credits_minor"),
            "history": versions,
        }

    missing = (
        s.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(
                *base,
                *settled_conditions(),
                or_(
                    Transaction.reporting_facts.is_(None),
                    Transaction.reporting_facts["scheme"].astext.is_(None),
                ),
            )
        )
        if account_id == COMBINED
        else 0
    )
    unmatched = (
        s.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(
                *base,
                *included,
                *settled_conditions(),
                Transaction.reporting_facts["scheme"].astext == "monzo_flex",
            )
        )
        if account_id == COMBINED
        else 0
    )
    return {
        "period": period,
        "start": start,
        "end": end,
        "account_id": account_id,
        "account_ids": ids,
        "currency": "GBP",
        "timezone": "Europe/London",
        "rollover": False,
        "revision": revision,
        "observed_at": observed,
        "editable": start.date() >= observed.astimezone(TZ).date().replace(day=1),
        "coverage": coverage(s, ids, start, end, observed),
        "evidence_missing_count": missing,
        "unmatched_flex_count": unmatched,
        "items": [item(c) for c in ("", *sorted({*categories, *history, "uncategorised"} - {""}))],
    }


def make_router(owner):
    router = APIRouter(prefix="/v1/dashboard", dependencies=[Depends(owner)])

    @router.get("/budgets")
    def read_budgets(
        period: str = Query(pattern=r"^(20\d{2}|2100)-(0[1-9]|1[0-2])$"),
        account_id: str = Query(COMBINED, max_length=128),
        currency: Literal["GBP"] = "GBP",
    ):
        return report(period, account_id)

    @router.put("/budgets")
    def edit_budget(edit: BudgetEdit):
        return save(edit)

    return router
