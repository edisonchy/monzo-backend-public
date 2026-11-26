"""Conservative deterministic candidates. No categorisation, fraud or subscription claims."""

import hashlib
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select

from monzo_backend.models import Transaction
from monzo_backend.reporting import settled_conditions

from .contracts import PatternResult, Scope
from .cursors import ToolFailure, canonical
from .reporting import GROUP_KEY, GROUP_NAME, TZ, joined


class Patterns:
    def __init__(self, reports):
        self.r = reports

    def load(self, scope):
        filters = self.r.filters(scope) + settled_conditions() + self.r.included(scope)
        if not self.r.metadata(scope).complete:
            raise ToolFailure(
                "insufficient_data",
                "Pattern analysis needs evidenced coverage for every selected account and date. Legacy imported history may have unknown coverage.",
            )
        rows = self.r.s.execute(
            joined(
                select(
                    Transaction.id,
                    Transaction.created_at,
                    Transaction.amount,
                    GROUP_KEY,
                    GROUP_NAME,
                )
            )
            .where(
                *filters,
                Transaction.amount != 0,
                Transaction.id.not_in(self.r.excluded),
            )
            .order_by(Transaction.created_at, Transaction.id)
            .limit(10001)
        ).all()
        if len(rows) > 10000:
            raise ToolFailure(
                "validation_error",
                "Pattern analysis is limited to 10,000 matching payments. Narrow the scope.",
            )
        return rows

    def result(self, tool, f, findings):
        for finding in findings:
            finding["id"] = hashlib.sha256(
                canonical([finding["kind"], finding["evidence_ids"]]).encode()
            ).hexdigest()
        findings.sort(key=lambda v: v["id"])
        count = len(findings)
        fp, position = self.r.page(tool, f)
        if position:
            findings = [v for v in findings if v["id"] > position[0]]
        more, page = len(findings) > f.limit, findings[: f.limit]
        cursor = self.r.cursors.encode(fp, self.r.revision, [page[-1]["id"]]) if more else None
        meta = self.r.metadata(f)
        meta.limitations.append(
            "Deterministic candidates, not confirmed subscriptions, fraud or future commitments. Inspect evidence before acting."
        )
        return PatternResult(metadata=meta, findings=page, matching_count=count, next_cursor=cursor)

    @staticmethod
    def finding(kind, rows, explanation, **extra):
        return {
            "id": "",
            "kind": kind,
            "direction": "out"
            if all(r[2] < 0 for r in rows)
            else "in"
            if all(r[2] > 0 for r in rows)
            else "mixed",
            "merchant_group_key": rows[0][3],
            "merchant": rows[0][4],
            "evidence_ids": [r[0] for r in rows[:100]],
            "occurrence_count": len(rows),
            "first_date": rows[0][1].astimezone(TZ).date().isoformat(),
            "last_date": rows[-1][1].astimezone(TZ).date().isoformat(),
            "amount_minor": abs(rows[-1][2]),
            "explanation": explanation,
            **extra,
        }

    def recurring(self, f):
        rows = self.load(f)
        groups = defaultdict(list)
        for row in rows:
            if f.direction == "out" and row[2] > 0 or f.direction == "in" and row[2] < 0:
                continue
            # Exact amounts deliberately separate concurrent plans at one merchant.
            # Variable bills remain unclassified until a separate, tested rule exists.
            groups[(row[3], row[2])].append(row)
        findings = []
        for series in groups.values():
            if len(series) < 3:
                continue
            days = [r[1].astimezone(TZ).date() for r in series]
            gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
            cadence, spacing, tolerance = None, None, None
            for name, length, allowed in (
                ("weekly", 7, 1),
                ("fortnightly", 14, 1),
                ("monthly", 30, 3),
                ("quarterly", 91, 4),
                ("yearly", 365, 2),
            ):
                if all(abs(gap - length) <= allowed for gap in gaps):
                    cadence, spacing, tolerance = name, length, allowed
                    break
            if cadence is None:
                continue
            predicted = days[-1] + timedelta(days=spacing)
            findings.append(
                self.finding(
                    "recurring_candidate",
                    series,
                    "At least three same-amount payments with regular intervals. This can also be repeated shopping; evidence IDs show at most the first 100 occurrences.",
                    minimum_minor=abs(series[0][2]),
                    maximum_minor=abs(series[0][2]),
                    cadence=cadence,
                    next_date_window=(
                        (predicted - timedelta(days=tolerance)).isoformat(),
                        (predicted + timedelta(days=tolerance)).isoformat(),
                    ),
                )
            )
        return self.result("get_recurring_payments", f, findings)

    @staticmethod
    def threshold(values):
        middle = median([Decimal(v) for v in values])
        mad = Decimal(median([abs(Decimal(v) - middle) for v in values]))
        # With MAD=0, require both a doubling and ten currency units above median.
        return middle + 3 * Decimal("1.4826") * mad if mad else max(middle * 2, middle + 1000)

    def unusual(self, f):
        baseline_scope = Scope.model_validate(
            {
                **f.model_dump(include=set(Scope.model_fields)),
                "date_from": f.baseline_from,
                "date_to": f.baseline_to,
            }
        )
        baseline, rows = self.load(baseline_scope), self.load(f)
        past = defaultdict(list)
        for row in baseline:
            past[(row[3], row[2] < 0)].append(abs(row[2]))
        baseline_groups = {key[0] for key in past}
        findings = []
        new_groups, duplicates, daily = defaultdict(list), defaultdict(list), defaultdict(list)
        for row in rows:
            peers = past[(row[3], row[2] < 0)]
            if "large_payment" in f.kinds and len(peers) >= 10:
                threshold = self.threshold(peers)
                if abs(row[2]) > threshold:
                    findings.append(
                        self.finding(
                            "large_payment",
                            [row],
                            "Payment exceeds the robust same-merchant/direction baseline threshold.",
                            baseline_observations=len(peers),
                            threshold_minor=str(threshold),
                        )
                    )
            if row[3] not in baseline_groups:
                new_groups[row[3]].append(row)
            if "possible_duplicate" in f.kinds:
                key = (row[3], row[2])
                previous = duplicates[key]
                if previous and row[1] - previous[-1][1] <= timedelta(hours=24):
                    findings.append(
                        self.finding(
                            "possible_duplicate",
                            [previous[-1], row],
                            "Same merchant and signed amount within 24 hours. May be intentional; known paired internal movements excluded.",
                        )
                    )
                previous.append(row)
            if row[2] < 0:
                daily[row[1].astimezone(TZ).date()].append(row)
        if "new_merchant" in f.kinds:
            for group in new_groups.values():
                findings.append(
                    self.finding(
                        "new_merchant",
                        group,
                        "Merchant absent from the available baseline, not necessarily first-ever. Evidence IDs show at most 100 payments.",
                    )
                )
        if "spending_spike" in f.kinds:
            totals = defaultdict(int)
            for row in baseline:
                if row[2] < 0:
                    totals[row[1].astimezone(TZ).date()] += abs(row[2])
            days = (f.baseline_to - f.baseline_from).days + 1
            values = [totals[f.baseline_from + timedelta(days=i)] for i in range(days)]
            threshold = self.threshold(values)
            for group in daily.values():
                amount = sum(abs(row[2]) for row in group)
                if amount > threshold:
                    findings.append(
                        self.finding(
                            "spending_spike",
                            group,
                            "Daily spending exceeds the covered daily baseline threshold, including covered zero-spend days. Evidence IDs show at most 100 payments.",
                            merchant_group_key=None,
                            merchant=None,
                            amount_minor=amount,
                            baseline_observations=days,
                            threshold_minor=str(threshold),
                        )
                    )
        result = self.result("find_unusual_activity", f, findings)
        result.metadata.limitations.append(
            "Payment-size outliers need ten same-merchant/direction baseline payments; groups below this threshold are not assessed for that rule."
        )
        return result
