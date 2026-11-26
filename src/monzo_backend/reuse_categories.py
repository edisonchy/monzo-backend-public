"""Apply saved merchant results to unresolved payments, without any provider requests."""

from sqlalchemy import or_, select

from monzo_backend.card_checks import CARD_CHECK
from monzo_backend.classifier import (
    Decision,
    cache_keys,
    cached_decision,
    cached_research,
    decision_result,
)
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import deterministic
from monzo_backend.models import Audit, Transaction, now
from monzo_backend.spending import eligible_accounts


def reuse_saved_categories():
    cfg = settings()
    updated, unmatched = 0, 0
    with session() as s, s.begin():
        rows = s.scalars(
            select(Transaction)
            .where(
                Transaction.account_id.in_(eligible_accounts()),
                Transaction.category_status == "needs_review",
                ~CARD_CHECK,
                Transaction.category.is_(None),
                or_(Transaction.category_source.is_(None), Transaction.category_source != "manual"),
                Transaction.declined.is_(False),
            )
            .order_by(Transaction.id)
            .with_for_update(skip_locked=True)
        ).all()
        for tx in rows:
            result = deterministic(s, tx)
            if not result and tx.merchant_name.strip():
                decisions, researches = cache_keys(s, tx, cfg)
                research = cached_research(researches)
                cached = cached_decision(s, decisions)
                if research:
                    result = {
                        "category": research["finding"].get("category"),
                        "source": "ai",
                        "status": "needs_review",
                    }
                elif cached and (cached.expires_at is None or cached.expires_at > now()):
                    decision = Decision.model_validate(cached.decision)
                    result = decision_result(decision, cfg, cache_hit=True)
                    # Showing a stored suggestion is not owner approval, even if the
                    # automatic-acceptance setting changes later.
                    result["status"] = "needs_review"
            if not result or not result.get("category"):
                unmatched += 1
                continue
            # Validate saved research too; malformed records roll back the operation.
            Decision(category=result["category"], needs_review=result["status"] == "needs_review")
            tx.category = result["category"]
            tx.category_source = result["source"]
            tx.category_status = result["status"]
            tx.version += 1  # Invalidate stale approvals and in-flight classification results.
            s.add(
                Audit(
                    actor="owner",
                    action="saved_category_reused",
                    detail={
                        "transaction_id": tx.id,
                        "category": tx.category,
                        "source": tx.category_source,
                        "status": tx.category_status,
                        "version": tx.version,
                    },
                )
            )
            updated += 1
        s.add(
            Audit(
                actor="owner",
                action="saved_categories_reused",
                detail={"updated": updated, "unmatched": unmatched},
            )
        )
    return {"updated": updated, "unmatched": unmatched, "provider_requests": 0}


if __name__ == "__main__":
    print(reuse_saved_categories())
