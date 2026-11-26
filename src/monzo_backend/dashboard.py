"""Owner-only reporting and review endpoints for the private dashboard."""

import base64
import binascii
import calendar
import json
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, case, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, OperationalError

from monzo_backend import (
    balances,
    card_checks,
    category_changes,
    custom_categories,
    merchant_groups,
    quality,
)
from monzo_backend.card_checks import CARD_CHECK, is_card_check
from monzo_backend.classifier import displayed_research
from monzo_backend.config import settings
from monzo_backend.db import is_transaction_contention, session
from monzo_backend.domain import CATEGORIES
from monzo_backend.models import (
    Account,
    Audit,
    CustomCategory,
    MerchantGroup,
    MerchantIdentity,
    Rule,
    Transaction,
    now,
    uid,
)
from monzo_backend.payment_purpose import confirmed_purpose
from monzo_backend.reporting import EFFECTIVE, money_totals, settled_conditions
from monzo_backend.spending import (
    ACCOUNT_KINDS,
    COMBINED,
    DISPLAY_MERCHANT,
    display_payment_name,
    eligible_accounts,
    exclusions,
)


class MerchantMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(max_length=36)
    source_id: str = Field(max_length=36)
    target_version: int = Field(ge=1)
    source_version: int = Field(ge=1)
    rule_policy: Literal["target", "source"] | None = None


TZ = ZoneInfo("Europe/London")


def bounds(period, mode):
    try:
        year, month = map(int, period.split("-"))
        if not 2000 <= year <= 2100 or not 1 <= month <= 12:
            raise ValueError()
        if mode == "year":
            month = 1
            start, end = datetime(year, 1, 1, tzinfo=TZ), datetime(year + 1, 1, 1, tzinfo=TZ)
            previous = datetime(year - 1, 1, 1, tzinfo=TZ)
        else:
            start = datetime(year, month, 1, tzinfo=TZ)
            end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=TZ)
            previous = datetime(year - (month == 1), 12 if month == 1 else month - 1, 1, tzinfo=TZ)
        return start, end, previous
    except (ValueError, TypeError):
        raise HTTPException(422, "Choose a valid month between 2000 and 2100") from None


def scope(
    account_id: str,
    period: str = Query(pattern=r"^\d{4}-\d{2}$"),
    mode: Literal["month", "year"] = "month",
    currency: str = Query("GBP", pattern=r"^[A-Z]{3}$"),
    category: str | None = None,
    merchant: str | None = Query(None, max_length=500),
):
    start, end, previous = bounds(period, mode)
    if category and category not in (*CATEGORIES, "uncategorised"):
        with session() as s:
            if not custom_categories.known(s, category):
                raise HTTPException(422, "Unknown category")
    return {
        "account_id": account_id,
        "currency": currency,
        "start": start,
        "end": end,
        "previous": previous,
        "mode": mode,
        "category": category,
        "merchant": merchant,
    }


def conditions(f, start=None, end=None):
    result = [
        Transaction.account_id.in_(eligible_accounts())
        if f["account_id"] == COMBINED
        else Transaction.account_id == f["account_id"],
        Transaction.currency == f["currency"],
    ]
    if not f.get("all_time"):
        result += [
            Transaction.created_at >= (start or f["start"]),
            Transaction.created_at < (end or f["end"]),
        ]
    if f["category"]:
        result.append(EFFECTIVE == f["category"])
    if f["merchant"] is not None:
        result.append(DISPLAY_MERCHANT == f["merchant"])
    return result


def check_account(s, account_id):
    if account_id == COMBINED:
        if not s.scalar(eligible_accounts().limit(1)):
            raise HTTPException(404, "No supported accounts connected")
        return None
    account = s.get(Account, account_id)
    if not account or account.kind not in ACCOUNT_KINDS:
        raise HTTPException(404, "Account not found")
    return account


def tx_json(t, excluded=None):
    return {
        "id": t.id,
        "account_id": t.account_id,
        "merchant": display_payment_name(t, (excluded or {}).get(t.id)),
        "merchant_id": t.merchant_id,
        "category_identity_id": t.category_identity,
        "directional_group": bool(t.payment_group_identity),
        "description": t.description,
        "amount_minor": t.amount,
        "currency": t.currency,
        "created_at": t.created_at,
        "settled_at": t.settled_at,
        "declined": t.declined,
        "bank_category": t.bank_category,
        "category": t.category,
        "category_source": t.category_source,
        "purpose_confirmation": confirmed_purpose(t),
        "category_status": t.category_status,
        "version": t.version,
        "card_verification": is_card_check(t),
        "combined_exclusion": (excluded or {}).get(t.id),
        "reporting_evidence_available": bool((t.reporting_facts or {}).get("scheme")),
    }


class Approval(BaseModel):
    category: str
    expected_version: int
    future_rule: bool = False
    expected_rule_category: str | None = None


class CardVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


class RuleEdit(BaseModel):
    category: str
    expected_category: str


def make_router(owner):
    router = APIRouter(prefix="/v1/dashboard", dependencies=[Depends(owner)])

    @router.get("/balances")
    def balance_report():
        return balances.report()

    @router.get("/bootstrap")
    def bootstrap():
        with session() as s:
            accounts = s.scalars(
                select(Account)
                .where(Account.kind.in_(ACCOUNT_KINDS))
                .order_by(Account.kind, Account.id)
            ).all()
            local_date = func.timezone("Europe/London", Transaction.created_at)
            year = func.extract("year", local_date).cast(Integer)
            recorded: dict[str, dict[str, list[int]]] = {}
            for account_id, currency, value in s.execute(
                select(Transaction.account_id, Transaction.currency, year).distinct().order_by(year)
            ):
                recorded.setdefault(account_id, {}).setdefault(currency, []).append(value)
            custom = dict(
                s.execute(
                    select(CustomCategory.key, CustomCategory.colour).order_by(
                        CustomCategory.identity
                    )
                ).all()
            )
            return {
                "today": now().astimezone(TZ).date(),
                "categories": [*CATEGORIES, *custom],
                "category_colours": custom,
                "default_account_id": COMBINED if accounts else None,
                "accounts": [
                    {
                        "id": a.id,
                        "kind": a.kind,
                        "history_status": a.history_status,
                        "currencies": sorted(recorded.get(a.id, {})) or ["GBP"],
                        "recorded_years": recorded.get(a.id, {}),
                    }
                    for a in accounts
                ],
            }

    @router.post("/categories")
    def create_category(body: custom_categories.NewCategory):
        with session() as s, s.begin():
            return custom_categories.create(s, body)

    @router.get("/categories")
    def category_usage(category: str):
        with session() as s:
            return custom_categories.usage(s, category)

    @router.delete("/categories")
    def delete_category(body: custom_categories.CategoryRemoval):
        try:
            with session() as s, s.begin():
                return custom_categories.remove(s, body)
        except OperationalError as exc:
            if is_transaction_contention(exc):
                raise HTTPException(409, "Another payment edit is in progress. Try again.") from exc
            raise

    @router.get("/report")
    def report(f: dict = Depends(scope)):
        with session() as s:
            s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            account = check_account(s, f["account_id"])
            combined = f["account_id"] == COMBINED
            excluded = exclusions(s) if combined else {}
            included = [Transaction.id.not_in(excluded)] if excluded else []
            eligible = settled_conditions()

            def period_data(start, end):
                base = conditions(f, start, end) + eligible
                filters = base + included
                debit = case((Transaction.amount < 0, -Transaction.amount), else_=0)
                total = money_totals(s, filters)
                local = func.timezone("Europe/London", Transaction.created_at)
                bucket = func.extract("month" if f["mode"] == "year" else "day", local).cast(
                    Integer
                )
                rows = s.execute(
                    select(bucket, func.sum(debit)).where(*filters).group_by(bucket)
                ).all()
                size = (
                    12 if f["mode"] == "year" else calendar.monthrange(start.year, start.month)[1]
                )
                values = {int(k): int(v) for k, v in rows}
                series = [values.get(i, 0) for i in range(1, size + 1)]
                category_rows = s.execute(
                    select(bucket, EFFECTIVE, -func.sum(Transaction.amount))
                    .where(*filters, Transaction.amount < 0)
                    .group_by(bucket, EFFECTIVE)
                    .order_by(bucket, (-func.sum(Transaction.amount)).desc(), EFFECTIVE)
                ).all()
                bucket_categories = [[] for _ in range(size)]
                for bucket_index, category, _amount in category_rows:
                    bucket_categories[int(bucket_index) - 1].append(category)
                excluded_rows = s.execute(
                    select(func.count(), func.coalesce(func.sum(debit), 0)).where(
                        *base, Transaction.id.in_(excluded)
                    )
                ).one()
                missing = (
                    s.scalar(
                        select(func.count())
                        .select_from(Transaction)
                        .where(
                            *base,
                            or_(
                                Transaction.reporting_facts.is_(None),
                                Transaction.reporting_facts["scheme"].astext.is_(None),
                            ),
                        )
                    )
                    if combined
                    else 0
                )
                unmatched = (
                    s.scalar(
                        select(func.count())
                        .select_from(Transaction)
                        .where(
                            *filters, Transaction.reporting_facts["scheme"].astext == "monzo_flex"
                        )
                    )
                    if combined
                    else 0
                )
                return {
                    "excluded_count": excluded_rows[0],
                    "excluded_debits_minor": int(excluded_rows[1]),
                    "evidence_missing_count": missing,
                    "unmatched_flex_count": unmatched,
                    "debits_minor": int(total[0]),
                    "credits_minor": int(total[1]),
                    "count": total[2],
                    "series": series,
                    "bucket_categories": bucket_categories,
                }

            current = period_data(f["start"], f["end"])
            previous = period_data(f["previous"], f["start"])
            groups = {}
            for name, column in (
                ("categories", EFFECTIVE),
                ("merchants", DISPLAY_MERCHANT),
            ):
                rows = s.execute(
                    select(
                        column,
                        func.count(),
                        -func.sum(Transaction.amount),
                        func.sum(case((Transaction.category_status == "needs_review", 1), else_=0)),
                    )
                    .where(*conditions(f), *eligible, *included, Transaction.amount < 0)
                    .group_by(column)
                    .order_by((-func.sum(Transaction.amount)).desc(), column)
                ).all()
                groups[name] = [
                    {
                        "key": k,
                        "count": count,
                        "amount_minor": int(amount),
                        "needs_review_count": int(review),
                    }
                    for k, count, amount, review in rows
                ]
            review_count = s.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(*conditions(f), Transaction.category_status == "needs_review", ~CARD_CHECK)
            )
            local_now = now().astimezone(TZ)
            visible = (
                0
                if local_now < f["start"]
                else (
                    len(current["series"])
                    if local_now >= f["end"]
                    else (local_now.month if f["mode"] == "year" else local_now.day)
                )
            )
            return {
                "currency": f["currency"],
                "current": current,
                "previous": previous,
                **groups,
                "review_count": review_count,
                "visible_buckets": visible,
                "history_status": account.history_status
                if account
                else (
                    "imported"
                    if all(
                        a.history_status == "imported"
                        for a in s.scalars(select(Account).where(Account.kind.in_(ACCOUNT_KINDS)))
                    )
                    else "incomplete"
                ),
                "combined": combined,
                "interpretation": (
                    "Combined settled spending across Current account and Flex. Confirmed own-account transfers, verified pot transfers and paired Flex movements are excluded; unmatched movements remain included. Refunds stay separate as money in."
                    if combined
                    else "Settled money out; transfers and Flex repayments included. Money in includes refunds and transfers."
                ),
            }

    @router.get("/transactions")
    def payments(
        f: dict = Depends(scope),
        q: str = Query("", max_length=200),
        status: Literal["all", "needs_review", "assigned", "pending", "excluded"] = "all",
        source: Literal["all", "ai", "manual", "rule", "monzo", "review"] = "all",
        settlement: Literal["all", "settled", "pending", "declined"] = "all",
        all_time: bool = False,
        cursor_mode: bool = False,
        cursor: str | None = Query(None, max_length=1000),
        categories: list[str] = Query(default=[], max_length=50),
        merchants: list[str] = Query(default=[], max_length=100),
        accounts: list[str] = Query(default=[], max_length=20),
        min_amount: int | None = Query(None, ge=0, le=100000000000),
        max_amount: int | None = Query(None, ge=0, le=100000000000),
        date_from: date | None = None,
        date_to: date | None = None,
        outgoing_only: bool = False,
        spending_only: bool = False,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        if any(c not in (*CATEGORIES, "uncategorised") for c in categories):
            with session() as s:
                if set(categories) - {*custom_categories.keys(s), "uncategorised"}:
                    raise HTTPException(422, "Unknown category")
        if any(len(m) > 500 for m in merchants):
            raise HTTPException(422, "Merchant name too long")
        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            raise HTTPException(422, "Minimum amount exceeds maximum")
        if cursor and not cursor_mode:
            raise HTTPException(422, "Cursor mode is required")
        after = None
        if cursor:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                stamp, tx_id = raw
                stamp = datetime.fromisoformat(stamp)
                if not stamp.tzinfo or not isinstance(tx_id, str) or not 1 <= len(tx_id) <= 128:
                    raise ValueError()
                after = (stamp, tx_id)
            except (ValueError, TypeError, binascii.Error, UnicodeError):
                raise HTTPException(422, "Invalid transaction cursor") from None
        f = {**f, "all_time": all_time}
        if date_from is not None or date_to is not None:
            if (
                date_from is None
                or date_to is None
                or date_from > date_to
                or date_from.year < 2000
                or date_to > now().astimezone(TZ).date()
            ):
                raise HTTPException(422, "Choose a valid date range between 2000 and today")
            f = {
                **f,
                "all_time": False,
                "start": datetime.combine(date_from, time.min, TZ),
                "end": datetime.combine(date_to + timedelta(days=1), time.min, TZ),
            }
        with session() as s:
            s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            check_account(s, f["account_id"])
            excluded = exclusions(s)
            filters = conditions(f)
            for account_id in accounts:
                check_account(s, account_id)
                if account_id == COMBINED:
                    raise HTTPException(422, "Choose individual accounts")
            if accounts:
                filters.append(Transaction.account_id.in_(accounts))
            if categories:
                filters.append(EFFECTIVE.in_(categories))
            if merchants:
                filters.append(DISPLAY_MERCHANT.in_(merchants))
            if min_amount is not None:
                filters.append(func.abs(Transaction.amount) >= min_amount)
            if max_amount is not None:
                filters.append(func.abs(Transaction.amount) <= max_amount)
            if spending_only:
                filters += [
                    Transaction.amount < 0,
                    Transaction.declined.is_(False),
                    Transaction.settled_at.is_not(None),
                ]
                if f["account_id"] == COMBINED and excluded:
                    filters.append(Transaction.id.not_in(excluded))
            if q:
                filters.append(
                    or_(
                        DISPLAY_MERCHANT.icontains(q, autoescape=True),
                        Transaction.description.icontains(q, autoescape=True),
                    )
                )
            if status == "needs_review" or spending_only:
                filters.append(~CARD_CHECK)
            if status != "all":
                filters.append(Transaction.category_status == status)
            if source != "all":
                filters.append(Transaction.category_source == source)
            if settlement == "declined":
                filters.append(Transaction.declined.is_(True))
            elif settlement != "all":
                filters += [
                    Transaction.declined.is_(False),
                    Transaction.settled_at.is_not(None)
                    if settlement == "settled"
                    else Transaction.settled_at.is_(None),
                ]
            if outgoing_only:
                filters.append(Transaction.amount < 0)
            count = s.scalar(select(func.count()).select_from(Transaction).where(*filters))
            page_filters = list(filters)
            if after:
                stamp, tx_id = after
                page_filters.append(
                    or_(
                        Transaction.created_at < stamp,
                        (Transaction.created_at == stamp) & (Transaction.id > tx_id),
                    )
                )
            rows = s.scalars(
                select(Transaction)
                .where(*page_filters)
                .order_by(Transaction.created_at.desc(), Transaction.id)
                .offset(0 if cursor_mode else offset)
                .limit(limit + 1 if cursor_mode else limit)
            ).all()
            has_more = cursor_mode and len(rows) > limit
            rows = rows[:limit]
            next_cursor = None
            if has_more:
                last = rows[-1]
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps([last.created_at.isoformat(), last.id]).encode()
                ).decode()
            return {
                "items": [tx_json(t, excluded) for t in rows],
                "total": count,
                "offset": 0 if cursor_mode else offset,
                "limit": limit,
                "next_cursor": next_cursor,
            }

    @router.get("/quality")
    def category_quality():
        with session() as s:
            return quality.summary(s)

    @router.get("/review-merchants")
    def review_merchants(
        sort: Literal["name", "frequency", "value"] = "name",
        currency: str = Query("GBP", pattern=r"^[A-Z]{3}$"),
        tab: Literal["ai", "needs_review"] = "ai",
        q: str = Query("", max_length=200),
        cursor: str | None = Query(None, max_length=3000),
        limit: int = Query(25, ge=1, le=100),
    ):
        identity = case(
            (Transaction.payment_group_identity.is_not(None), Transaction.payment_group_identity),
            else_=func.coalesce("group:" + MerchantIdentity.group_id, "payment:" + Transaction.id),
        )
        direction_label = case(
            (
                Transaction.payment_group_identity.is_not(None),
                case((Transaction.amount < 0, " · Money out"), else_=" · Money in"),
            ),
            else_="",
        )
        with session() as s:
            excluded = exclusions(s)
            filters = [
                Transaction.account_id.in_(eligible_accounts()),
                Transaction.declined.is_(False),
                ~CARD_CHECK,
                or_(
                    Transaction.category_status == "needs_review",
                    (Transaction.category_status == "assigned")
                    & (Transaction.category_source == "ai"),
                ),
            ]
            if excluded:
                filters.append(Transaction.id.not_in(excluded))
            if sort == "value":
                filters.append(Transaction.currency == currency)
            grouped = (
                select(
                    identity.label("key"),
                    func.min(
                        func.coalesce(MerchantGroup.name, DISPLAY_MERCHANT) + direction_label
                    ).label("name"),
                    func.min(Transaction.id).label("payment_id"),
                    func.count().label("payment_count"),
                    func.sum(case((Transaction.amount < 0, -Transaction.amount), else_=0)).label(
                        "debits_minor"
                    ),
                    func.min(Transaction.category).label("category"),
                    (
                        func.min(
                            case(
                                (
                                    (Transaction.category_source == "ai")
                                    & Transaction.category.in_(CATEGORIES),
                                    1,
                                ),
                                else_=0,
                            )
                        )
                        == 1
                    )
                    .__and__(func.count(func.distinct(Transaction.category)) == 1)
                    .label("suggested"),
                )
                .outerjoin(
                    MerchantIdentity, MerchantIdentity.merchant_id == Transaction.category_identity
                )
                .outerjoin(MerchantGroup, MerchantGroup.id == MerchantIdentity.group_id)
                .where(*filters)
                .group_by(identity)
                .subquery()
            )
            name = func.coalesce(func.nullif(grouped.c.name, ""), "Unnamed payment")
            order_name = func.lower(name)
            query = select(grouped, name.label("display_name")).where(
                grouped.c.suggested.is_(tab == "ai")
            )
            if q:
                query = query.where(name.icontains(q, autoescape=True))
            metric = grouped.c.payment_count if sort == "frequency" else grouped.c.debits_minor
            if cursor and sort != "name":
                try:
                    after = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    if (
                        not isinstance(after, list)
                        or len(after) != 6
                        or after[:4] != [sort, currency, q, tab]
                        or type(after[4]) is not int
                        or after[4] < 0
                        or not isinstance(after[5], str)
                    ):
                        raise ValueError()
                except (ValueError, TypeError, binascii.Error, UnicodeError):
                    raise HTTPException(422, "Invalid review cursor") from None
                query = query.where(
                    or_(metric < after[4], (metric == after[4]) & (grouped.c.key > after[5]))
                )
            elif cursor:
                try:
                    after = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    if (
                        not isinstance(after, list)
                        or len(after) != 2
                        or not all(isinstance(v, str) for v in after)
                    ):
                        raise ValueError()
                except (ValueError, TypeError, binascii.Error, UnicodeError):
                    raise HTTPException(422, "Invalid review cursor") from None
                query = query.where(
                    or_(
                        order_name > after[0], (order_name == after[0]) & (grouped.c.key > after[1])
                    )
                )
            rows = (
                s.execute(
                    query.order_by(
                        order_name if sort == "name" else metric.desc(), grouped.c.key
                    ).limit(limit + 1)
                )
                .mappings()
                .all()
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "items": [
                    {
                        "key": r["key"],
                        "name": r["display_name"],
                        "payment_id": r["payment_id"],
                        "payment_count": r["payment_count"],
                        "debits_minor": int(r["debits_minor"]) if sort == "value" else None,
                        "currency": currency if sort == "value" else None,
                        "category": r["category"] if r["suggested"] else None,
                    }
                    for r in rows
                ],
                "next_cursor": base64.urlsafe_b64encode(
                    json.dumps(
                        [rows[-1]["display_name"].lower(), rows[-1]["key"]]
                        if sort == "name"
                        else [
                            sort,
                            currency,
                            q,
                            tab,
                            int(
                                rows[-1]["payment_count"]
                                if sort == "frequency"
                                else rows[-1]["debits_minor"]
                            ),
                            rows[-1]["key"],
                        ]
                    ).encode()
                ).decode()
                if has_more
                else None,
            }

    @router.get("/merchant-groups")
    def list_merchant_groups(q: str = Query("", max_length=200), offset: int = Query(0, ge=0)):
        with session() as s:
            query = (
                select(MerchantGroup, Rule.category, func.count(Transaction.id))
                .join(MerchantIdentity, MerchantIdentity.group_id == MerchantGroup.id)
                .join(Transaction, Transaction.category_identity == MerchantIdentity.merchant_id)
                .outerjoin(Rule, Rule.group_id == MerchantGroup.id)
                .where(
                    MerchantGroup.merged_into.is_(None),
                    Transaction.account_id.in_(eligible_accounts()),
                )
                .group_by(MerchantGroup.id, Rule.category)
                .order_by(func.lower(MerchantGroup.name), MerchantGroup.id)
            )
            if q:
                query = query.where(MerchantGroup.name.icontains(q, autoescape=True))
            rows = s.execute(query.offset(offset).limit(51)).all()
            return {
                "items": [
                    {
                        "id": g.id,
                        "name": g.name,
                        "version": g.version,
                        "category": c,
                        "payment_count": n,
                    }
                    for g, c, n in rows[:50]
                ],
                "has_more": len(rows) > 50,
            }

    @router.post("/merchant-groups/merge")
    def merge_merchant_groups(body: MerchantMerge):
        with session() as s, s.begin():
            return merchant_groups.merge(s, **body.model_dump())

    @router.get("/merchant-options")
    def merchant_options(q: str = Query("", max_length=200), offset: int = Query(0, ge=0)):
        with session() as s:
            filters = [Transaction.account_id.in_(eligible_accounts())]
            if q:
                filters.append(DISPLAY_MERCHANT.icontains(q, autoescape=True))
            rows = s.scalars(
                select(DISPLAY_MERCHANT)
                .where(*filters)
                .distinct()
                .order_by(DISPLAY_MERCHANT)
                .offset(offset)
                .limit(101)
            ).all()
            return {"items": rows[:100], "has_more": len(rows) > 100}

    @router.get("/transactions/{tx_id}")
    def payment(tx_id: str):
        with session() as s:
            tx = s.get(Transaction, tx_id)
            if not tx:
                raise HTTPException(404, "Payment not found")
            check_account(s, tx.account_id)
            research = displayed_research(s, tx, settings())
            rule = (
                s.scalar(select(Rule).where(Rule.merchant_id == tx.category_identity))
                if tx.category_identity
                else None
            )
            group = merchant_groups.for_merchant(s, tx.category_identity)
            if group and not tx.payment_group_identity:
                rule = s.scalar(select(Rule).where(Rule.group_id == group.id)) or rule
            return {
                **tx_json(tx, exclusions(s)),
                "rule": {"category": rule.category} if rule else None,
                "research": {
                    "finding": research["finding"],
                    "created_at": research["created_at"],
                    "model": research["model"],
                }
                if research
                else None,
            }

    @router.post("/transactions/{tx_id}/category-preview")
    def category_preview(tx_id: str, body: category_changes.CategoryChoice):
        with session() as s, s.begin():
            s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            return category_changes.preview(s, tx_id, body)

    @router.put("/transactions/{tx_id}/category-change")
    def category_change(tx_id: str, body: category_changes.CategoryApply):
        try:
            with session() as s, s.begin():
                return category_changes.apply(s, tx_id, body)
        except (IntegrityError, OperationalError) as error:
            if getattr(error.orig, "sqlstate", None) in {"23505", "40001", "40P01", "55P03"}:
                raise HTTPException(
                    409, "Payments or rules are being updated; reopen the picker"
                ) from None
            raise

    @router.post("/category-changes/{batch_id}/undo")
    def undo_category_change(batch_id: str):
        with session() as s, s.begin():
            return category_changes.undo(s, batch_id)

    @router.put("/transactions/{tx_id}/card-verification")
    def confirm_card_verification(tx_id: str, body: CardVerification):
        with session() as s, s.begin():
            return card_checks.confirm(s, tx_id, body.expected_version)

    @router.put("/transactions/{tx_id}/approve")
    def approve(tx_id: str, body: Approval):
        with session() as s, s.begin():
            if not custom_categories.known(s, body.category):
                raise HTTPException(422, "Unknown category")
            merchant_groups.lock(s)
            initial = s.get(Transaction, tx_id)
            if initial:
                category_changes.merchant_lock(s, initial.category_identity)
            tx = s.scalar(
                select(Transaction)
                .where(Transaction.id == tx_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not tx:
                raise HTTPException(404, "Payment not found")
            check_account(s, tx.account_id)
            if tx.version != body.expected_version:
                raise HTTPException(409, "Payment changed; reload before saving")
            if is_card_check(tx):
                raise HTTPException(409, "Card verifications do not need a spending category")
            if tx.declined:
                raise HTTPException(409, "Declined payments cannot be approved")
            if body.future_rule and not tx.category_identity:
                raise HTTPException(422, "No merchant ID is available for this payment")
            if body.future_rule:
                # Serialise the absent-row case too, including approvals of different payments
                # for the same merchant. Optimistic comparison prevents stale rule replacement.
                s.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:merchant, 0))"),
                    {"merchant": tx.category_identity},
                )
                existing = s.scalar(
                    select(Rule).where(Rule.merchant_id == tx.category_identity).with_for_update()
                )
                if (existing.category if existing else None) != body.expected_rule_category:
                    raise HTTPException(409, "Merchant rule changed; reload before saving")
                if existing:
                    existing.category = body.category
                    existing.effective_from = now()
                else:
                    created = s.scalar(
                        insert(Rule)
                        .values(
                            id=uid(),
                            merchant_id=tx.category_identity,
                            category=body.category,
                            effective_from=now(),
                        )
                        .on_conflict_do_nothing(index_elements=[Rule.merchant_id])
                        .returning(Rule.id)
                    )
                    if not created:
                        raise HTTPException(409, "Merchant rule changed; reload before saving")
                s.add(
                    Audit(
                        actor="owner",
                        action="merchant_rule_saved",
                        detail={
                            "merchant_id": tx.category_identity,
                            "category": body.category,
                            "scope": "future_classifications",
                        },
                    )
                )
            s.add(
                Audit(
                    actor="owner",
                    action="category_corrected",
                    detail={
                        "transaction_id": tx.id,
                        "previous_category": tx.category,
                        "category": body.category,
                    },
                )
            )
            tx.category, tx.category_source, tx.category_status = (
                body.category,
                "manual",
                "assigned",
            )
            tx.version += 1
            return {"id": tx.id, "version": tx.version, "future_rule": body.future_rule}

    @router.get("/rules")
    def rules():
        with session() as s:
            rows = s.execute(
                select(
                    Rule.id,
                    Rule.merchant_id,
                    Rule.category,
                    func.coalesce(MerchantGroup.name, func.min(DISPLAY_MERCHANT)),
                )
                .outerjoin(Transaction, Transaction.category_identity == Rule.merchant_id)
                .outerjoin(MerchantGroup, MerchantGroup.id == Rule.group_id)
                .group_by(Rule.id, MerchantGroup.name)
                .order_by(Rule.id)
            ).all()
            return [
                {
                    "id": i,
                    "merchant_id": m,
                    "category": c,
                    "merchant": (n or "Unnamed merchant")
                    + (
                        " · Money in"
                        if m and m.startswith("payment_group_") and m.endswith("_in")
                        else " · Money out"
                        if m and m.startswith("payment_group_")
                        else ""
                    ),
                }
                for i, m, c, n in rows
            ]

    @router.put("/rules/{rule_id}")
    def edit_rule(rule_id: str, body: RuleEdit):
        with session() as s, s.begin():
            if not custom_categories.known(s, body.category):
                raise HTTPException(422, "Unknown category")
            merchant_groups.lock(s)
            rule = s.scalar(select(Rule).where(Rule.id == rule_id).with_for_update())
            if not rule:
                raise HTTPException(404, "Rule not found")
            if rule.group_id:
                s.get(MerchantGroup, rule.group_id).version += 1
            if rule.category != body.expected_category:
                raise HTTPException(409, "Rule changed; reload first")
            s.add(
                Audit(
                    actor="owner",
                    action="merchant_rule_edited",
                    detail={
                        "merchant_id": rule.merchant_id,
                        "previous_category": rule.category,
                        "category": body.category,
                    },
                )
            )
            rule.category = body.category
            rule.effective_from = now()
        return {"status": "saved", "scope": "future_classifications"}

    @router.delete("/rules/{rule_id}")
    def disable_rule(rule_id: str, expected_category: str):
        with session() as s, s.begin():
            merchant_groups.lock(s)
            rule = s.scalar(select(Rule).where(Rule.id == rule_id).with_for_update())
            if not rule:
                raise HTTPException(404, "Rule not found")
            if rule.group_id:
                s.get(MerchantGroup, rule.group_id).version += 1
            if rule.category != expected_category:
                raise HTTPException(409, "Rule changed; reload first")
            s.add(
                Audit(
                    actor="owner",
                    action="merchant_rule_disabled",
                    detail={"merchant_id": rule.merchant_id, "previous_category": rule.category},
                )
            )
            s.delete(rule)
        return {"status": "disabled", "scope": "future_classifications"}

    return router
