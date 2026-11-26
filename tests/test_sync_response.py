from datetime import timedelta

import pytest
from sqlalchemy import select

from monzo_backend import monzo, worker
from monzo_backend.db import session
from monzo_backend.domain import enqueue
from monzo_backend.http import ServiceError
from monzo_backend.models import Account, Connection, Job, ReportingCoverage, Transaction, now


def test_fetch_rejects_another_payment_on_same_account(monkeypatch, payment):
    monkeypatch.setattr(monzo, "bank_get", lambda *args: {"transaction": payment})
    with session() as s, s.begin():
        job_id = enqueue(
            s,
            "sync",
            "fetch_transaction",
            "requested",
            {"account_id": "acc_test", "transaction_id": "tx_requested"},
        )

    assert worker.tick("sync")
    with session() as s:
        job = s.get(Job, job_id)
        assert job.state == "failed"
        assert job.error_code == "monzo_transaction_id_mismatch"
        assert s.scalar(select(Transaction.id)) is None
        assert s.scalar(select(Job.id).where(Job.id != job_id)) is None


def sync_page():
    end = now()
    return {
        "id": "page",
        "kind": "sync_page",
        "payload": {
            "account_id": "acc_test",
            "since": (end - timedelta(days=1)).isoformat(),
            "before": end.isoformat(),
        },
    }


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"transactions": {}},
        {"transactions": ""},
        {"transactions": [None]},
        {"transactions": [{}] * 101},
    ],
)
def test_malformed_page_cannot_prove_coverage(monkeypatch, response):
    monkeypatch.setattr(monzo, "bank_get", lambda *args: response)

    with pytest.raises(ServiceError, match="monzo_transactions_response_invalid"):
        monzo.execute(sync_page())

    with session() as s:
        assert s.scalar(select(ReportingCoverage.id)) is None
        assert s.get(Account, "acc_test").last_sync_at is None
        assert s.scalar(select(Job.id)) is None


def test_valid_empty_page_proves_coverage(monkeypatch):
    monkeypatch.setattr(monzo, "bank_get", lambda *args: {"transactions": []})
    monzo.execute(sync_page())
    with session() as s:
        assert s.scalar(select(ReportingCoverage.id)) is not None


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"accounts": {}},
        {"accounts": ""},
        {"accounts": [None]},
        {"accounts": [{"id": " ", "type": "uk_retail"}]},
        {"accounts": [{"id": "new", "type": "uk_retail"}, {"id": "bad"}]},
    ],
)
def test_reconnect_rejects_malformed_accounts_without_applying_discovery(monkeypatch, response):
    monkeypatch.setattr(monzo, "bank_get", lambda *_: response)
    with session() as s, s.begin():
        s.get(Connection, 1).state = "reauthorisation_required"
        job_id = enqueue(s, "sync", "discover", "reconnect", {"reconnect": True})
    assert worker.tick("sync")
    with session() as s:
        job = s.get(Job, job_id)
        assert (job.state, job.error_code) == ("failed", "monzo_accounts_response_invalid")
        assert s.get(Connection, 1).state == "reauthorisation_required"
        assert s.scalar(select(Job.id).where(Job.id != job_id)) is None
        assert s.get(Account, "new") is None
