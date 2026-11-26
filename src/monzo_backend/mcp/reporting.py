"""Bounded read-only queries over the same reporting semantics as the dashboard."""

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Date, case, func, or_, select, tuple_

from monzo_backend.card_checks import CARD_CHECK, is_card_check
from monzo_backend.domain import CATEGORIES
from monzo_backend.models import (
    Account,
    DashboardRevision,
    MerchantGroup,
    MerchantIdentity,
    ReportingCoverage,
    Rule,
    Transaction,
    now,
)
from monzo_backend.payment_purpose import confirmed_purpose
from monzo_backend.reporting import CREDIT, DEBIT, EFFECTIVE, money_totals, settled_conditions
from monzo_backend.spending import ACCOUNT_KINDS, DISPLAY_MERCHANT, display_payment_name, exclusions

from .contracts import (
    BreakdownResult,
    CompareResult,
    ContextResult,
    DetailResult,
    Metadata,
    Money,
    QualityResult,
    Scope,
    SearchResult,
    SeriesResult,
    SummaryResult,
)
from .cursors import ToolFailure

TZ = ZoneInfo("Europe/London")
GROUP_KEY = func.coalesce(
    "group:" + MerchantIdentity.group_id,
    "merchant:" + func.nullif(Transaction.category_identity, ""),
    "payment:" + Transaction.id,
)
GROUP_NAME = func.coalesce(MerchantGroup.name, func.nullif(DISPLAY_MERCHANT, ""), "Unnamed payment")
DIMENSIONS = {
    "category": (EFFECTIVE, EFFECTIVE),
    "account": (Transaction.account_id, Transaction.account_id),
    "merchant_group": (GROUP_KEY, GROUP_NAME),
}
CAPABILITIES = [
    "get_balances",
    "get_budget_status",
    "get_data_context",
    "get_summary",
    "get_breakdown",
    "get_time_series",
    "compare_periods",
    "search_transactions",
    "get_transaction",
    "get_category_quality",
    "get_recurring_payments",
    "find_unusual_activity",
]


def joined(stmt):
    return (
        stmt.select_from(Transaction)
        .outerjoin(MerchantIdentity, Transaction.category_identity == MerchantIdentity.merchant_id)
        .outerjoin(MerchantGroup, MerchantIdentity.group_id == MerchantGroup.id)
    )


def stamp(day):
    return datetime.combine(day, time.min, TZ)


def ratio(numerator, denominator):
    return (
        str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.01")))
        if denominator
        else None
    )


class Reports:
    def __init__(self, s, cursors):
        self.s, self.cursors = s, cursors
        self.revision = (
            s.scalar(select(DashboardRevision.revision).where(DashboardRevision.id == 1)) or 0
        )
        self.accounts = s.scalars(
            select(Account)
            .where(
                Account.kind.in_(ACCOUNT_KINDS),
                Account.connection_id == 1,
            )
            .order_by(Account.id)
        ).all()
        self.account_ids = [a.id for a in self.accounts]
        self.excluded = exclusions(s, account_ids=self.account_ids)
        self.observed_at = now()
        self.coverage = {}
        self.coverage_observed = {}
        # Merge overlapping windows in PostgreSQL; do not materialise every sync
        # observation or re-sort the full history once per chart bucket.
        rows = s.execute(
            select(
                ReportingCoverage.account_id,
                func.range_agg(
                    func.tstzrange(ReportingCoverage.start_at, ReportingCoverage.end_at, "[)")
                ),
                func.max(ReportingCoverage.observed_at),
            )
            .where(ReportingCoverage.account_id.in_(self.account_ids))
            .group_by(ReportingCoverage.account_id)
        )
        for aid, intervals, observed in rows:
            self.coverage[aid] = [(r.lower, r.upper) for r in intervals]
            self.coverage_observed[aid] = observed

    def check_revision(self, expected):
        if expected is not None and expected != self.revision:
            raise ToolFailure("data_changed", "Data changed. Start a fresh analysis.")

    def ids(self, f):
        ids = f.account_ids or self.account_ids
        if not ids or set(ids) - set(self.account_ids):
            raise ToolFailure("validation_error", "Choose supported connected accounts.")
        return ids

    def filters(self, f):
        self.check_revision(f.expected_revision)
        ids = self.ids(f)
        result = [
            Transaction.account_id.in_(ids),
            Transaction.currency == f.currency,
            Transaction.created_at >= stamp(f.date_from),
            Transaction.created_at < stamp(f.date_to + timedelta(days=1)),
        ]
        if f.categories:
            result.append(EFFECTIVE.in_(f.categories))
        if f.merchant_group_ids:
            known = set(
                self.s.scalars(
                    select(MerchantGroup.id).where(
                        MerchantGroup.id.in_(f.merchant_group_ids),
                        MerchantGroup.merged_into.is_(None),
                        MerchantGroup.id.in_(
                            select(MerchantIdentity.group_id)
                            .join(
                                Transaction,
                                Transaction.category_identity == MerchantIdentity.merchant_id,
                            )
                            .where(Transaction.account_id.in_(self.account_ids))
                        ),
                    )
                )
            )
            if known != set(f.merchant_group_ids):
                raise ToolFailure("validation_error", "Unknown or merged merchant group.")
            result.append(
                Transaction.category_identity.in_(
                    select(MerchantIdentity.merchant_id).where(
                        MerchantIdentity.group_id.in_(f.merchant_group_ids)
                    )
                )
            )
        if f.category_status != "all":
            result.append(Transaction.category_status == f.category_status)
        return result

    def included(self, f):
        combined = not f.account_ids or len(f.account_ids) > 1
        return [Transaction.id.not_in(self.excluded)] if combined and self.excluded else []

    def covered(self, account_id, start, end):
        return any(a <= start and b >= end for a, b in self.coverage.get(account_id, []))

    def metadata(self, f=None, *, basis=None):
        if f:
            self.check_revision(f.expected_revision)
        ids = self.ids(f) if isinstance(f, Scope) else self.account_ids
        coverage = []
        for aid in ids:
            intervals = self.coverage.get(aid, [])
            observed = self.coverage_observed.get(aid)
            complete = isinstance(f, Scope) and self.covered(
                aid, stamp(f.date_from), stamp(f.date_to + timedelta(days=1))
            )
            coverage.append(
                {
                    "account_id": aid,
                    "status": "complete" if complete else "partial" if intervals else "unknown",
                    "intervals": [(a.isoformat(), b.isoformat()) for a, b in intervals],
                    "latest_observation": observed.isoformat() if observed else None,
                    "checked_through": intervals[-1][1].isoformat() if intervals else None,
                }
            )
        complete = bool(coverage) and all(c["status"] == "complete" for c in coverage)
        limitations = [
            "Money in is not reliably classified as income or refunds.",
            "AI suggestions are not verified categories.",
        ]
        if not complete:
            limitations.append(
                "History coverage is incomplete or unknown; missing dates are not zero spending."
            )
        return Metadata(
            effective_filters=f.model_dump(mode="json", exclude={"cursor"}) if f else {},
            reporting_basis=basis
            or (
                "combined_settled_spending"
                if not f or not getattr(f, "account_ids", []) or len(f.account_ids) > 1
                else "single_account_settled_movements_including_transfers"
            ),
            currency=getattr(f, "currency", None),
            generated_at=self.observed_at.isoformat(),
            data_revision=self.revision,
            latest_sync={
                a.id: a.last_sync_at.isoformat() if a.last_sync_at else None
                for a in self.accounts
                if a.id in ids
            },
            coverage=coverage,
            complete=complete,
            limitations=limitations,
        )

    def summary(self, f):
        base = self.filters(f)
        included = self.included(f)
        eligible = base + settled_conditions()
        filters = eligible + included
        debit, credit, count = money_totals(self.s, filters)
        outgoing = self.s.scalar(
            select(func.count()).select_from(Transaction).where(*filters, Transaction.amount < 0)
        )
        excluded = (
            money_totals(self.s, eligible + [Transaction.id.in_(self.excluded)])
            if included
            else (0, 0, 0)
        )
        pending = money_totals(
            self.s,
            base
            + [~CARD_CHECK, Transaction.declined.is_(False), Transaction.settled_at.is_(None)]
            + included,
        )
        declined = self.s.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(*base, Transaction.declined.is_(True))
        )
        return SummaryResult(
            metadata=self.metadata(f),
            totals=Money(
                spending_minor=int(debit),
                money_in_minor=int(credit),
                count=count,
                outgoing_count=outgoing,
                mean_outgoing_minor=ratio(debit, outgoing),
                excluded_count=excluded[2],
                excluded_outgoing_minor=int(excluded[0]),
                pending_outgoing_minor=int(pending[0]),
                pending_count=pending[2],
                declined_count=declined,
            ),
        )

    def context(self, f):
        self.check_revision(f.expected_revision)
        currencies = self.s.execute(
            select(Transaction.account_id, Transaction.currency)
            .where(Transaction.account_id.in_(self.account_ids))
            .distinct()
        ).all()
        return ContextResult(
            metadata=self.metadata(f),
            accounts=[
                {
                    "id": a.id,
                    "kind": a.kind,
                    "currencies": sorted(c for aid, c in currencies if aid == a.id),
                }
                for a in self.accounts
            ],
            categories=list(CATEGORIES) + ["uncategorised"],
            capabilities=CAPABILITIES,
            definitions={
                "balances": "Stored GBP Current available balance and separate active pots only. Flex balances, aggregate savings, net worth and forecasts are unsupported; check source age and outcome.",
                "budgets": "GBP London calendar months, no rollover. Targets overlap; remaining is target minus settled spending, with pending and credits separate. Missing targets and incomplete history are not a safe-to-spend allowance. Read-only through MCP.",
                "combined": "Settled debits excluding card checks, owner-confirmed own-account transfers and bank-evidenced pot/Flex movements; unmatched movements included.",
                "single_account": "Settled movements including transfers and Flex repayments.",
                "money_in": "Positive movements, not necessarily income. Refunds remain separate from spending.",
                "time_basis": "Transaction creation time; inclusive London dates; Monday-start weeks.",
                "verification": "Assigned bank/manual/rule decisions are distinct from needs_review AI suggestions.",
                "coverage": "Only fully paginated, recent accessible sync windows are evidenced. Legacy imported history stays unknown.",
            },
        )

    def page(self, tool, f):
        fingerprint = self.cursors.fingerprint(tool, f)
        position = self.cursors.decode(f.cursor, fingerprint, self.revision)
        return fingerprint, position

    def breakdown(self, f):
        filters = self.filters(f) + settled_conditions() + self.included(f)
        if f.metric == "spending":
            filters.append(Transaction.amount < 0)
        elif f.metric == "money_in":
            filters.append(Transaction.amount > 0)
        columns = [DIMENSIONS[d][0].label(f"key{i}") for i, d in enumerate(f.dimensions)]
        labels = [func.min(DIMENSIONS[d][1]).label(f"label{i}") for i, d in enumerate(f.dimensions)]
        metric = (
            func.count()
            if f.metric == "count"
            else func.sum(DEBIT if f.metric == "spending" else CREDIT)
        )
        grouped = (
            joined(select(*columns, *labels, metric.label("value"), func.count().label("count")))
            .where(*filters)
            .group_by(*columns)
            .subquery()
        )
        total, count = self.s.execute(
            select(func.coalesce(func.sum(grouped.c.value), 0), func.count()).select_from(grouped)
        ).one()
        fp, position = self.page("get_breakdown", f)
        order_value = -grouped.c.value if f.sort == "largest" else grouped.c.value
        keys = [grouped.c[f"key{i}"] for i in range(len(columns))]
        query = select(grouped).order_by(order_value, *keys)
        if position:
            query = query.where(tuple_(order_value, *keys) > tuple(position))
        rows = self.s.execute(query.limit(f.limit + 1)).mappings().all()
        more = len(rows) > f.limit
        rows = rows[: f.limit]
        result = [
            {
                "keys": [r[f"key{i}"] for i in range(len(columns))],
                "labels": [r[f"label{i}"] for i in range(len(columns))],
                "value": int(r["value"]),
                "count": r["count"],
                "share_percent": ratio(r["value"] * 100, total),
            }
            for r in rows
        ]
        cursor = (
            self.cursors.encode(
                fp,
                self.revision,
                [(-1 if f.sort == "largest" else 1) * result[-1]["value"], *result[-1]["keys"]],
            )
            if more
            else None
        )
        return BreakdownResult(
            metadata=self.metadata(f),
            rows=result,
            denominator=int(total),
            matching_groups=count,
            next_cursor=cursor,
        )

    def payment(self, tx, group_id, *, detail=False, pending_card=False):
        exclusion = self.excluded.get(tx.id, {})
        reason = (
            "card_verification"
            if is_card_check(tx)
            else "declined"
            if tx.declined
            else "pending"
            if not tx.settled_at
            else exclusion.get("reason")
        )
        return {
            "id": tx.id,
            "pending_card": bool(
                pending_card and not exclusion and not tx.declined and tx.amount < 0
            ),
            "account_id": tx.account_id,
            "merchant": display_payment_name(tx, exclusion),
            "merchant_group_id": group_id,
            "merchant_id": tx.merchant_id,
            "category_identity_id": tx.category_identity,
            "directional_group": bool(tx.payment_group_identity),
            "amount_minor": tx.amount,
            "currency": tx.currency,
            "created_at": tx.created_at.isoformat(),
            "settled_at": tx.settled_at.isoformat() if tx.settled_at else None,
            "declined": tx.declined,
            "category": tx.category
            if tx.category_status in ("assigned", "needs_review") and tx.category in CATEGORIES
            else "uncategorised",
            "category_source": tx.category_source,
            "purpose_confirmation": confirmed_purpose(tx),
            "category_status": tx.category_status,
            "included_in_combined_spending": reason is None and tx.amount < 0,
            "exclusion_reason": reason,
            "matched_transaction_id": exclusion.get("matched_transaction_id"),
            "reporting_evidence_available": bool((tx.reporting_facts or {}).get("scheme")),
            "description": tx.description if detail else None,
            "version": tx.version,
            "category_edit_eligible": not is_card_check(tx) and not tx.declined,
            "category_edit_reason": "card_verification"
            if is_card_check(tx)
            else "declined"
            if tx.declined
            else None,
        }

    def search(self, f):
        filters = self.filters(f)
        if f.query:
            filters.append(
                or_(
                    Transaction.description.icontains(f.query, autoescape=True),
                    DISPLAY_MERCHANT.icontains(f.query, autoescape=True),
                )
            )
        if f.min_amount_minor is not None:
            filters.append(func.abs(Transaction.amount) >= f.min_amount_minor)
        if f.max_amount_minor is not None:
            filters.append(func.abs(Transaction.amount) <= f.max_amount_minor)
        if f.direction != "all":
            filters.append(
                Transaction.amount < 0 if f.direction == "out" else Transaction.amount > 0
            )
        if f.source != "all":
            filters.append(Transaction.category_source == f.source)
        if (
            f.mode == "summary"
            and f.reported_spending_only
            and f.direction in ("all", "out")
            and f.settlement in ("all", "settled")
        ):
            return self.spending_summary(f, filters)
        pending_cards = (
            f.reported_spending_only and f.mode == "payments" and f.settlement == "pending"
        )
        if pending_cards:
            filters += [
                self.active_card_hold(),
                Transaction.declined.is_(False),
                Transaction.amount < 0,
                ~CARD_CHECK,
                *self.included(f),
            ]
            cutoff = self.spending_cutoff(f, self.metadata(f))
            if cutoff:
                filters.append(Transaction.created_at < cutoff)
        elif f.reported_spending_only:
            filters += [*settled_conditions(), *self.included(f), Transaction.amount < 0]
        if f.settlement == "settled":
            filters += [Transaction.settled_at.is_not(None), Transaction.declined.is_(False)]
        elif f.settlement == "pending":
            filters += [Transaction.settled_at.is_(None), Transaction.declined.is_(False)]
        elif f.settlement == "declined":
            filters.append(Transaction.declined.is_(True))
        if f.mode == "summary":
            amount = -Transaction.amount
            count, total, largest = self.s.execute(
                select(func.count(), func.coalesce(func.sum(amount), 0), func.max(amount))
                .select_from(Transaction)
                .where(*filters)
            ).one()
            return SearchResult(
                metadata=self.metadata(f),
                rows=[],
                matching_count=count,
                next_cursor=None,
                summary={"recorded_total_minor": int(total), "largest_minor": largest},
            )
        count = self.s.scalar(select(func.count()).select_from(Transaction).where(*filters))
        fp, position = self.page("search_transactions", f)
        order = (Transaction.created_at, Transaction.id)
        if position:
            position = (datetime.fromisoformat(position[0]), position[1])
            filters.append(
                tuple_(*order) < position if f.sort == "newest" else tuple_(*order) > position
            )
        rows = self.s.execute(
            joined(select(Transaction, MerchantIdentity.group_id))
            .where(*filters)
            .order_by(*(c.desc() if f.sort == "newest" else c.asc() for c in order))
            .limit(f.limit + 1)
        ).all()
        more, rows = len(rows) > f.limit, rows[: f.limit]
        result = [self.payment(tx, gid, pending_card=pending_cards) for tx, gid in rows]
        cursor = (
            self.cursors.encode(
                fp, self.revision, [rows[-1][0].created_at.isoformat(), rows[-1][0].id]
            )
            if more
            else None
        )
        return SearchResult(
            metadata=self.metadata(f, basis="all_account_movements"),
            rows=result,
            matching_count=count,
            next_cursor=cursor,
        )

    def spending_cutoff(self, f, meta):
        cutoff = None
        if not meta.complete and f.date_to == self.observed_at.astimezone(TZ).date():
            start = stamp(f.date_from)
            ends = [
                next(
                    (right for left, right in self.coverage.get(aid, []) if left <= start < right),
                    None,
                )
                for aid in self.ids(f)
            ]
            no_internal_gap = all(
                not any(
                    start < left < stamp(f.date_to + timedelta(days=1))
                    for left, _ in self.coverage.get(aid, [])
                )
                for aid in self.ids(f)
            )
            if no_internal_gap and all(ends):
                cutoff = min(ends)

        return cutoff

    @staticmethod
    def active_card_hold():
        pending = Transaction.settled_at.is_(None)
        scheme = Transaction.reporting_facts["scheme"].astext
        # A later completed bank scan over the creation time supersedes an old
        # hold that was absent from that scan. Ingest refreshes reporting_checked_at
        # for every observed row, even when the transaction itself did not change.
        absent = (
            select(ReportingCoverage.id)
            .where(
                ReportingCoverage.account_id == Transaction.account_id,
                ReportingCoverage.source == "monzo_recent_paginated",
                ReportingCoverage.start_at <= Transaction.created_at,
                ReportingCoverage.end_at > Transaction.created_at,
                ReportingCoverage.end_at > Transaction.reporting_checked_at,
            )
            .exists()
        )
        return (
            pending
            & (scheme == "mastercard")
            & Transaction.reporting_checked_at.is_not(None)
            & ~absent
        )

    def spending_summary(self, f, filters):
        meta = self.metadata(f)
        cutoff = self.spending_cutoff(f, meta)
        if cutoff:
            filters.append(Transaction.created_at < cutoff)
        settled = Transaction.settled_at.is_not(None)
        pending = Transaction.settled_at.is_(None)
        scheme = Transaction.reporting_facts["scheme"].astext
        card = self.active_card_hold()
        unknown = pending & (
            scheme.is_(None)
            | (scheme == "")
            | ((scheme == "mastercard") & Transaction.reporting_checked_at.is_(None))
        )
        amount = -Transaction.amount
        count, total, largest, held, holds, uncertain = self.s.execute(
            select(
                func.count().filter(settled),
                func.sum(amount).filter(settled),
                func.max(amount).filter(settled),
                func.sum(amount).filter(card),
                func.count().filter(card),
                func.count().filter(unknown),
            )
            .select_from(Transaction)
            .where(
                *filters,
                *self.included(f),
                Transaction.amount < 0,
                Transaction.declined.is_(False),
                ~CARD_CHECK,
            )
        ).one()
        return SearchResult(
            metadata=meta,
            matching_count=count,
            rows=[],
            next_cursor=None,
            summary=dict(
                recorded_total_minor=int(total or 0),
                largest_minor=largest,
                pending_card_minor=None if uncertain else int(held or 0),
                pending_card_count=None if uncertain else holds,
                verified_until=cutoff.isoformat() if cutoff else None,
            ),
        )

    def detail(self, f):
        self.check_revision(f.expected_revision)
        row = self.s.execute(
            joined(select(Transaction, MerchantIdentity.group_id, self.active_card_hold())).where(
                Transaction.id == f.transaction_id, Transaction.account_id.in_(self.account_ids)
            )
        ).first()
        if not row:
            raise ToolFailure("validation_error", "Payment not found in supported accounts.")
        meta = self.metadata(f, basis="payment_facts_and_combined_inclusion")
        meta.currency = row[0].currency
        return DetailResult(
            metadata=meta, payment=self.payment(row[0], row[1], detail=True, pending_card=row[2])
        )

    def series(self, f):
        filters = self.filters(f) + settled_conditions() + self.included(f)
        day = func.timezone("Europe/London", Transaction.created_at).cast(Date)
        rows = self.s.execute(
            select(
                day,
                func.sum(DEBIT),
                func.sum(CREDIT),
                func.count(),
                func.sum(case((Transaction.category_status == "needs_review", 1), else_=0)),
                func.sum(case((EFFECTIVE == "uncategorised", 1), else_=0)),
            )
            .where(*filters)
            .group_by(day)
            .order_by(day)
        ).all()
        by_day = {r[0]: r[1:] for r in rows}
        buckets, profiles = {}, defaultdict(list)
        current = f.date_from
        while current <= f.date_to:
            complete = all(
                self.covered(aid, stamp(current), stamp(current + timedelta(days=1)))
                for aid in self.ids(f)
            )
            values = by_day.get(current)
            key = (
                current
                if f.grain == "day"
                else current - timedelta(days=current.weekday())
                if f.grain == "week"
                else current.replace(day=1)
            )
            bucket = buckets.setdefault(
                key,
                {
                    "date": key.isoformat(),
                    "spending_minor": 0,
                    "money_in_minor": 0,
                    "count": 0,
                    "suggested_count": 0,
                    "uncategorised_count": 0,
                    "coverage_complete": True,
                    "has_data": False,
                },
            )
            bucket["coverage_complete"] &= complete
            if values or complete:
                bucket["has_data"] = True
                for name, value in zip(
                    (
                        "spending_minor",
                        "money_in_minor",
                        "count",
                        "suggested_count",
                        "uncategorised_count",
                    ),
                    values or (0, 0, 0, 0, 0),
                    strict=True,
                ):
                    bucket[name] += int(value)
            if f.profile and complete:
                profiles[current.isoweekday() if f.profile == "weekday" else current.day].append(
                    int(values[0]) if values else 0
                )
            current += timedelta(days=1)
        if len(buckets) > 1000:
            raise ToolFailure(
                "validation_error",
                "Choose a coarser grain or shorter date range (maximum 1,000 buckets).",
            )
        output, cumulative, cumulative_complete = [], defaultdict(int), True
        for bucket in buckets.values():
            has_data = bucket.pop("has_data")
            cumulative_complete &= bucket["coverage_complete"]
            if not has_data:
                continue
            if f.cumulative:
                for name in (
                    "spending_minor",
                    "money_in_minor",
                    "count",
                    "suggested_count",
                    "uncategorised_count",
                ):
                    cumulative[name] += bucket[name]
                    bucket[name] = cumulative[name]
                bucket["coverage_complete"] = cumulative_complete
            output.append(bucket)
        return SeriesResult(
            metadata=self.metadata(f),
            buckets=output,
            profile=[
                {
                    "key": key,
                    "observed_days": len(values),
                    "mean_spending_minor": ratio(sum(values), len(values)),
                }
                for key, values in sorted(profiles.items())
            ],
        )

    def compare(self, f):
        selected, previous = f.selected, f.previous
        if f.alignment == "elapsed_period":
            days = min(
                (selected.date_to - selected.date_from).days,
                (previous.date_to - previous.date_from).days,
            )
            selected = selected.model_copy(
                update={"date_to": selected.date_from + timedelta(days=days)}
            )
            previous = previous.model_copy(
                update={"date_to": previous.date_from + timedelta(days=days)}
            )
        a, b = self.summary(selected), self.summary(previous)
        delta = a.totals.spending_minor - b.totals.spending_minor
        contributors = []
        if f.attribution:
            key, label = DIMENSIONS[f.attribution]
            totals = []
            for scope in (selected, previous):
                rows = self.s.execute(
                    joined(select(key, func.min(label), func.sum(DEBIT)))
                    .where(
                        *self.filters(scope),
                        *settled_conditions(),
                        *self.included(scope),
                        Transaction.amount < 0,
                    )
                    .group_by(key)
                ).all()
                totals.append({k: (name, int(value)) for k, name, value in rows})
            for k in set(totals[0]) | set(totals[1]):
                av, bv = totals[0].get(k), totals[1].get(k)
                contributors.append(
                    {
                        "key": k,
                        "label": (av or bv)[0],
                        "difference_minor": (av[1] if av else 0) - (bv[1] if bv else 0),
                    }
                )
            contributors.sort(key=lambda r: (-abs(r["difference_minor"]), r["key"]))
        meta = self.metadata(selected)
        meta.effective_filters = f.model_dump(mode="json")
        meta.complete = a.metadata.complete and b.metadata.complete
        if not meta.complete and not any("History coverage" in item for item in meta.limitations):
            meta.limitations.append("Previous-period history coverage is incomplete or unknown.")
        return CompareResult(
            metadata=meta,
            selected=a,
            previous=b,
            difference_minor=delta,
            change_percent=ratio(delta * 100, b.totals.spending_minor),
            contributors=contributors[:10],
        )

    def quality(self, f):
        quality_key = case(
            (
                Transaction.payment_group_identity.is_not(None),
                "merchant:" + Transaction.payment_group_identity,
            ),
            else_=GROUP_KEY,
        )
        quality_name = GROUP_NAME + case(
            (
                Transaction.payment_group_identity.is_not(None),
                case((Transaction.amount < 0, " · Money out"), else_=" · Money in"),
            ),
            else_="",
        )
        filters = self.filters(f) + [~CARD_CHECK, Transaction.declined.is_(False)]
        suggested = Transaction.category_status == "needs_review"
        unknown = EFFECTIVE == "uncategorised"
        missing = or_(
            Transaction.reporting_facts.is_(None),
            Transaction.reporting_facts["scheme"].astext.is_(None),
        )
        aggregate = (
            joined(
                select(
                    quality_key.label("key"),
                    func.min(quality_name).label("name"),
                    func.count().label("count"),
                    func.sum(case((suggested, 1), else_=0)).label("suggested"),
                    func.sum(case((unknown, 1), else_=0)).label("unknown"),
                    func.sum(case((missing, 1), else_=0)).label("missing"),
                    func.sum(DEBIT).label("amount"),
                    func.array_agg(func.distinct(case((suggested, EFFECTIVE)))).label("categories"),
                )
            )
            .where(*filters)
            .group_by(quality_key)
            .subquery()
        )
        count = self.s.scalar(select(func.count()).select_from(aggregate))
        fp, position = self.page("get_category_quality", f)
        query = select(aggregate).order_by(aggregate.c.key)
        if position:
            query = query.where(aggregate.c.key > position[0])
        rows = self.s.execute(query.limit(f.limit + 1)).mappings().all()
        more, rows = len(rows) > f.limit, rows[: f.limit]
        group_ids = [r["key"][6:] for r in rows if r["key"].startswith("group:")]
        merchant_ids = [r["key"][9:] for r in rows if r["key"].startswith("merchant:")]
        members = dict(
            self.s.execute(
                select(MerchantIdentity.merchant_id, MerchantIdentity.group_id).where(
                    MerchantIdentity.group_id.in_(group_ids)
                )
            ).all()
        )
        rules_by_key = defaultdict(list)
        for rule in self.s.scalars(
            select(Rule)
            .where(
                or_(
                    Rule.group_id.in_(group_ids),
                    Rule.merchant_id.in_([*merchant_ids, *members]),
                )
            )
            .order_by(Rule.id)
        ):
            key = (
                "group:" + rule.group_id
                if rule.group_id
                else "group:" + members[rule.merchant_id]
                if rule.merchant_id in members
                else "merchant:" + rule.merchant_id
            )
            rules_by_key[key].append(rule)
        result = []
        for r in rows:
            categories = sorted(c for c in r["categories"] if c)
            key = r["key"]
            rules = rules_by_key[key]
            result.append(
                {
                    "group_key": key,
                    "merchant": r["name"],
                    "count": r["count"],
                    "suggested_count": int(r["suggested"]),
                    "uncategorised_count": int(r["unknown"]),
                    "missing_evidence_count": int(r["missing"]),
                    "outgoing_minor": int(r["amount"]),
                    "suggested_categories": categories,
                    "conflicting_suggestions": len(categories) > 1,
                    "rules": [
                        {
                            "id": rule.id,
                            "merchant_id": rule.merchant_id,
                            "group_id": rule.group_id,
                            "category": rule.category,
                            "effective_from": rule.effective_from.isoformat()
                            if rule.effective_from
                            else None,
                        }
                        for rule in rules
                    ],
                }
            )
        cursor = self.cursors.encode(fp, self.revision, [rows[-1]["key"]]) if more else None
        return QualityResult(
            metadata=self.metadata(f, basis="category_quality_of_non_declined_movements"),
            rows=result,
            matching_groups=count,
            next_cursor=cursor,
        )
