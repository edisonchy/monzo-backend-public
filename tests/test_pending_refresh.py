from datetime import timedelta

from sqlalchemy import select

from monzo_backend.db import session
from monzo_backend.domain import claim, enqueue, ingest
from monzo_backend.http import ServiceError
from monzo_backend.models import Job, Notification, Transaction, now
from monzo_backend.monzo import execute, schedule_sync


def test_old_pending_schedule_is_bounded_persisted_and_fair(payment):
    with session() as s, s.begin():
        for i in range(25):
            ingest(
                s,
                "acc_test",
                {
                    **payment,
                    "id": f"tx_old{i}",
                    "created": (now() - timedelta(days=20)).isoformat(),
                },
            )
        ingest(s, "acc_test", {**payment, "id": "tx_recent", "created": now().isoformat()})
        schedule_sync(s)
    with session() as s, s.begin():
        jobs = list(s.scalars(select(Job).where(Job.kind == "fetch_transaction")))
        assert len(jobs) == 20
        schedule_sync(s)
    with session() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == "fetch_transaction")))
        assert len(jobs) == 25
        assert all(j.payload["transaction_id"] != "tx_recent" for j in jobs)
        assert s.get(Transaction, "tx_old0").settled_at is None


def test_unavailable_old_payment_preserves_state_and_backs_off(monkeypatch, payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)

    def denied(*_):
        raise ServiceError("monzo_http_403", False)

    monkeypatch.setattr("monzo_backend.monzo.bank_get", denied)
    execute(
        {
            "kind": "fetch_transaction",
            "payload": {
                "transaction_id": payment["id"],
                "account_id": "acc_test",
                "older_pending": True,
            },
        }
    )
    with session() as s:
        tx = s.get(Transaction, payment["id"])
        assert tx.pending_check_after > now() + timedelta(days=6)
        assert tx.settled_at is None
        assert s.scalar(select(Notification)) is not None


def test_targeted_fetch_priority_and_aged_reconciliation_fairness():
    with session() as s, s.begin():
        bulk = enqueue(s, "sync", "sync_page", "bulk", {})
        target = enqueue(s, "sync", "fetch_transaction", "target", {})
    with session() as s, s.begin():
        assert claim(s, "sync")["id"] == target
        s.get(Job, bulk).created_at = now() - timedelta(minutes=6)
        enqueue(s, "sync", "fetch_transaction", "target2", {})
    with session() as s, s.begin():
        assert claim(s, "sync")["id"] == bulk
