"""Review progress and evidenced coverage, without changing any category."""

from sqlalchemy import func, select

from monzo_backend.card_checks import CARD_CHECK
from monzo_backend.domain import deterministic
from monzo_backend.mcp.reporting import Reports
from monzo_backend.models import Transaction
from monzo_backend.payment_identity import PAYMENT_NAME, UNKNOWN
from monzo_backend.spending import eligible_accounts, exclusions


def summary(s):
    filters = [
        Transaction.account_id.in_(eligible_accounts()),
        Transaction.declined.is_(False),
        ~CARD_CHECK,
    ]
    excluded = exclusions(s)
    if excluded:
        filters.append(Transaction.id.not_in(excluded))
    counts = {"total": 0, "verified": 0, "ai_suggested": 0, "needs_review": 0, "pending": 0}
    for status, source, count in s.execute(
        select(Transaction.category_status, Transaction.category_source, func.count())
        .where(*filters)
        .group_by(Transaction.category_status, Transaction.category_source)
    ):
        counts["total"] += count
        key = (
            "verified"
            if status == "assigned" and source in ("manual", "rule", "monzo")
            else "ai_suggested"
            if source == "ai" and status in ("assigned", "needs_review")
            else "pending"
            if status == "pending"
            else "needs_review"
        )
        counts[key] += count
    unnamed = s.scalars(
        select(Transaction).where(
            *filters,
            func.length(func.trim(Transaction.merchant_name)) == 0,
            Transaction.category_status.in_(("pending", "needs_review")),
        )
    ).all()
    supported = sum(deterministic(s, tx) is not None for tx in unnamed)
    reports = Reports(s, None)
    return {
        **counts,
        "missing_payment_name": s.scalar(
            select(func.count()).select_from(Transaction).where(*filters, PAYMENT_NAME == UNKNOWN)
        ),
        "reference_only_name": s.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(*filters, PAYMENT_NAME.startswith("Payment reference: "))
        ),
        "unnamed": len(unnamed),
        "unnamed_with_bank_or_rule_evidence": supported,
        "unnamed_manual_review": len(unnamed) - supported,
        "coverage": [
            {
                "account_id": a.id,
                "kind": a.kind,
                "intervals": [[start, end] for start, end in reports.coverage.get(a.id, [])],
                "observed_at": reports.coverage_observed.get(a.id),
            }
            for a in reports.accounts
        ],
    }
