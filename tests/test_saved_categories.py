from datetime import timedelta

from sqlalchemy import func, select

from monzo_backend.classifier import decision_key
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import (
    Account,
    Audit,
    Job,
    MerchantDecision,
    MerchantResearch,
    Transaction,
    now,
)
from monzo_backend.research import merchant_key
from monzo_backend.reuse_categories import reuse_saved_categories


def test_reuse_saved_categories_is_exact_idempotent_and_keeps_approval_boundary(
    monkeypatch, payment
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved-only reuse must not contact a provider")

    monkeypatch.setattr("monzo_backend.classifier.request", forbidden)
    monkeypatch.setattr("monzo_backend.research.request", forbidden)
    with session() as s, s.begin():
        s.add(Account(id="rewards", connection_id=1, kind="uk_rewards"))
        s.flush()
        for txid, name, category, source, account in [
            ("match", "Saved Shop", None, "review", "acc_test"),
            ("manual", "Saved Shop", None, "manual", "acc_test"),
            ("existing", "Saved Shop", "bills", "ai", "acc_test"),
            ("similar", "Saved Shop UK", None, "review", "acc_test"),
            ("expired", "Expired Shop", None, "review", "acc_test"),
            ("research", "Research Shop", None, "review", "acc_test"),
            ("rewards", "Saved Shop", None, "review", "rewards"),
        ]:
            tx = ingest(
                s,
                account,
                {
                    **payment,
                    "id": txid,
                    "account_id": account,
                    "merchant": {"id": txid, "name": name},
                },
            )
            tx.category = category
            tx.category_source = source
            tx.category_status = "needs_review"
            if txid in ("match", "expired"):
                s.add(
                    MerchantDecision(
                        key=decision_key(tx, settings()),
                        decision={"category": "shopping", "needs_review": False},
                        expires_at=now() - timedelta(days=1) if txid == "expired" else None,
                    )
                )
        s.add(
            MerchantResearch(
                key=merchant_key("Research Shop"),
                merchant_name="Research Shop",
                finding={
                    "category": "groceries",
                    "needs_review": False,
                    "summary": "Saved",
                    "source_urls": [],
                },
                model="test",
            )
        )
        s.flush()
        jobs = s.scalar(select(func.count()).select_from(Job))
    assert reuse_saved_categories() == {"updated": 2, "unmatched": 2, "provider_requests": 0}
    assert reuse_saved_categories()["updated"] == 0
    with session() as s:
        tx = s.get(Transaction, "match")
        assert (tx.category, tx.category_status, tx.category_source, tx.version) == (
            "shopping",
            "needs_review",
            "ai",
            2,
        )
        assert s.get(Transaction, "research").category == "groceries"
        assert s.get(Transaction, "research").category_status == "needs_review"
        for txid in ["manual", "similar", "expired", "rewards"]:
            assert s.get(Transaction, txid).category is None
        assert s.get(Transaction, "existing").category == "bills"
        assert s.scalar(select(func.count()).select_from(Job)) == jobs
        assert (
            s.scalar(
                select(func.count())
                .select_from(Audit)
                .where(Audit.action == "saved_category_reused")
            )
            == 2
        )
