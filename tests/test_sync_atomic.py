from datetime import timedelta

import pytest
from sqlalchemy import select, text

from monzo_backend import domain, monzo, worker
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import enqueue
from monzo_backend.models import Account, Connection, Job, Notification, Transaction, now, uid


def queue_fetch():
    with session() as s, s.begin():
        return enqueue(
            s,
            "sync",
            "fetch_transaction",
            "target",
            {
                "account_id": "acc_test",
                "transaction_id": "tx_test",
            },
        )


def test_final_attempt_crash_after_saving_is_already_complete(monkeypatch, payment):
    job_id = queue_fetch()
    with session() as s, s.begin():
        s.get(Job, job_id).attempts = settings().max_attempts - 1
    monkeypatch.setattr(monzo, "bank_get", lambda *_: {"transaction": payment})

    def crash_after_save(work):
        monzo.execute(work)
        raise SystemExit

    monkeypatch.setattr(worker, "execute", crash_after_save)
    with pytest.raises(SystemExit):
        worker.tick("sync")
    with session() as s:
        assert s.get(Transaction, "tx_test")
        assert s.get(Job, job_id).state == "completed"
        assert s.scalar(select(Notification.id)) is None


@pytest.mark.parametrize("reclaimed", [True, False])
def test_lost_lease_during_bank_read_cannot_write(monkeypatch, payment, reclaimed):
    job_id = queue_fetch()

    def bank_get(*_):
        with session() as s, s.begin():
            job = s.get(Job, job_id)
            if reclaimed:
                job.lease_token = uid()
            else:
                job.lease_until = now() - timedelta(seconds=1)
        return {"transaction": payment}

    monkeypatch.setattr(monzo, "bank_get", bank_get)
    assert worker.tick("sync")
    with session() as s:
        assert s.get(Transaction, "tx_test") is None
        assert s.get(Job, job_id).state == "running"


@pytest.mark.parametrize("sqlstate", ["40001", "40P01", "55P03", "23514"])
def test_only_confirmed_transient_result_failures_retry(monkeypatch, payment, sqlstate):
    job_id = queue_fetch()
    monkeypatch.setattr(monzo, "bank_get", lambda *_: {"transaction": payment})
    ingest = monzo.ingest

    def abort_save(s, *args):
        ingest(s, *args)
        s.flush()
        s.execute(text("DO $$ BEGIN RAISE SQLSTATE '" + sqlstate + "'; END $$"))

    monkeypatch.setattr(monzo, "ingest", abort_save)
    assert worker.tick("sync")
    with session() as s, s.begin():
        job = s.get(Job, job_id)
        assert s.get(Transaction, "tx_test") is None
        if sqlstate == "23514":
            assert job.state == "failed"
            return
        assert job.state == "pending"
        assert job.attempts == 1
        assert job.available_at > now()
        job.available_at = now()
    monkeypatch.setattr(monzo, "ingest", ingest)
    assert worker.tick("sync")
    with session() as s:
        assert s.get(Job, job_id).state == "completed"
        assert s.get(Transaction, "tx_test")


@pytest.mark.parametrize("last_attempt", [False, True])
def test_locked_account_rolls_back_then_retries_within_job_budget(
    monkeypatch, payment, last_attempt
):
    job_id = queue_fetch()
    if last_attempt:
        with session() as s, s.begin():
            s.get(Job, job_id).payload = {
                **s.get(Job, job_id).payload,
                "webhook_retry_limit": 1,
            }

    monkeypatch.setattr(monzo, "bank_get", lambda *_: {"transaction": payment})
    original_ingest = monzo.ingest

    def fast_timeout(s, *args):
        s.execute(text("SET LOCAL lock_timeout = '50ms'"))
        return original_ingest(s, *args)

    monkeypatch.setattr(monzo, "ingest", fast_timeout)
    with session() as blocker, blocker.begin():
        blocker.scalar(select(Account).where(Account.id == "acc_test").with_for_update())
        assert worker.tick("sync")

    with session() as s, s.begin():
        job = s.get(Job, job_id)
        assert job.error_code == "sync_database_contention"
        assert job.attempts == 1
        assert s.scalar(select(Job.id).where(Job.id != job_id)) is None
        assert s.get(Transaction, "tx_test") is None
        if last_attempt:
            assert job.state == "failed"
            assert s.scalars(select(Notification.event_key)).all() == [f"sync-failed:{job_id}"]
            return
        assert job.state == "pending"
        assert s.scalar(select(Notification.id)) is None
        job.available_at = now()

    assert worker.tick("sync")
    with session() as s:
        assert s.get(Job, job_id).state == "completed"
        assert s.get(Transaction, "tx_test")
        assert sorted(s.scalars(select(Job.kind).where(Job.id != job_id))) == [
            "classify",
            "collect_balance",
            "collect_balance",
        ]


def test_crash_before_completion_rolls_back_payment_and_followups(monkeypatch, payment):
    job_id = queue_fetch()
    monkeypatch.setattr(monzo, "bank_get", lambda *_: {"transaction": payment})

    def stop_before_completion(s, *args, **kwargs):
        # The transaction has actually written its effects when process death is simulated.
        assert s.get(Transaction, "tx_test")
        assert s.scalar(select(Job.id).where(Job.id != job_id))
        raise SystemExit

    with monkeypatch.context() as patch:
        patch.setattr(domain, "finish", stop_before_completion)
        with pytest.raises(SystemExit):
            worker.tick("sync")

    with session() as s, s.begin():
        assert s.get(Transaction, "tx_test") is None
        assert s.scalar(select(Job.id).where(Job.id != job_id)) is None
        job = s.get(Job, job_id)
        assert job.state == "running"
        job.lease_until = now() - timedelta(seconds=1)
    assert worker.tick("sync")
    with session() as s:
        assert s.get(Job, job_id).state == "completed"
        assert s.get(Transaction, "tx_test")


def test_pre_request_lock_retries_without_contacting_monzo(monkeypatch, payment):
    job_id = queue_fetch()
    methods = []

    def request(method, *args, **kwargs):
        methods.append(method)
        return {"transaction": payment}

    monkeypatch.setattr(monzo, "request", request)
    with session() as s, s.begin():
        conn = s.get(Connection, 1)
        conn.access_token = monzo.encrypt("test-access")
        conn.expires_at = now() + timedelta(hours=1)
    with session() as blocker, blocker.begin():
        blocker.get(Connection, 1, with_for_update=True)
        assert worker.tick("sync")
    with session() as s, s.begin():
        job = s.get(Job, job_id)
        assert methods == []
        assert job.state == "pending" and job.error_code == "sync_database_contention"
        assert job.attempts == 1 and job.available_at > now()
        assert s.scalar(select(Notification.id)) is None
        job.available_at = now()
    assert worker.tick("sync")
    with session() as s:
        assert methods == ["GET"]
        assert s.get(Job, job_id).state == "completed"
        assert s.get(Transaction, payment["id"])
