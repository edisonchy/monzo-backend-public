from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from monzo_backend import balances, monzo
from monzo_backend.db import session
from monzo_backend.http import ServiceError
from monzo_backend.models import Account, BalanceObservation, BalanceSnapshot, Job, now
from monzo_backend.worker import tick


def pot(pid="pot_test", **extra):
    return {
        "id": pid,
        "name": "Holiday",
        "current_account_id": "acc_test",
        "balance": 0,
        "currency": "GBP",
        "deleted": False,
        **extra,
    }


def running(source="current"):
    with session() as s, s.begin():
        balances.schedule(s, "acc_test")
        # Select one source independently of UUID ordering.
        job = s.scalar(select(Job).where(Job.payload["source"].as_string() == source))
        job.state, job.lease_token = "running", "test-lease"
        job.lease_until = now() + timedelta(minutes=10)
        return {
            "id": job.id,
            "lease_token": job.lease_token,
            "payload": dict(job.payload),
            "kind": job.kind,
        }


def test_normalisation_preserves_zero_negative_and_ignores_unverified_totals():
    assert balances.normalise(
        "current", "acc_test", {"balance": -543, "currency": "GBP", "total_balance": 999999}
    ) == {"amount_minor": -543, "currency": "GBP", "basis": "available"}
    assert balances.normalise(
        "pots", "acc_test", {"pots": [pot(), pot("pot_deleted", deleted=True, balance=900)]}
    ) == {
        "pots": [
            {
                "id": "pot_test",
                "name": "Holiday",
                "amount_minor": 0,
                "currency": "GBP",
                "basis": "pot_balance",
            }
        ]
    }


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"balance": True, "currency": "GBP"},
        {"balance": 1.1, "currency": "GBP"},
        {"balance": 5, "currency": "USD"},
        {"balance": 2**53, "currency": "GBP"},
    ],
)
def test_invalid_current_response_is_not_zero(value):
    with pytest.raises(ServiceError, match="response_invalid"):
        balances.normalise("current", "acc_test", value)


@pytest.mark.parametrize(
    "value",
    [
        {"pots": [pot(current_account_id="another")]},
        {"pots": [pot(), pot()]},
        {"pots": [pot(currency="EUR")]},
        {"pots": [pot(deleted=None)]},
        {"pots": [pot(), pot("pot_bad", balance=None)]},
        {"pots": None},
    ],
)
def test_rejects_entire_mixed_or_incomplete_pot_response(value):
    with pytest.raises(ServiceError):
        balances.normalise("pots", "acc_test", value)


def test_schedule_coalesces_and_excludes_flex_disabled_other_owner(monkeypatch):
    instant = now()
    monkeypatch.setattr(balances, "now", lambda: instant)
    with session() as s, s.begin():
        s.add_all(
            [
                Account(id="flex", connection_id=1, kind="uk_monzo_flex"),
                Account(id="disabled", connection_id=1, kind="uk_retail", enabled=False),
            ]
        )
        first = balances.schedule(s, "acc_test")
        assert len(first) == 2
        assert balances.schedule(s, "acc_test") == first
        assert balances.schedule(s, "flex") == []
        assert balances.schedule(s, "disabled") == []
    monkeypatch.setattr(balances, "now", lambda: instant + timedelta(minutes=16))
    with session() as s, s.begin():
        assert balances.schedule(s, "acc_test") == first
        assert s.scalar(select(func.count()).select_from(Job)) == 2


def test_partial_failure_keeps_good_current_and_previous_pots(monkeypatch):
    current, pots = running(), running("pots")
    monkeypatch.setattr(
        monzo,
        "bank_get",
        lambda path, params: (
            {"balance": 0, "currency": "GBP"} if path == "/balance" else {"pots": [pot()]}
        ),
    )
    balances.collect(current)
    balances.collect(pots)

    def fail(*args):
        raise ServiceError("monzo_http_429", True, 120)

    monkeypatch.setattr(monzo, "bank_get", fail)
    with pytest.raises(ServiceError) as exc:
        balances.collect(pots)
    assert exc.value.retry_after == 120
    report = balances.report()
    assert report["items"][0]["state"] == "fresh"
    assert report["items"][0]["data"]["amount_minor"] == 0
    assert report["items"][1]["state"] == "stale"
    assert report["items"][1]["data"]["pots"][0]["amount_minor"] == 0
    assert len(report["items"][1]["history"]) == 1
    monkeypatch.setattr(monzo, "bank_get", lambda *args: {"pots": []})
    balances.collect(pots)
    assert balances.report()["items"][1]["data"] == {"pots": []}
    assert balances.report()["items"][1]["state"] == "fresh"


def test_lease_and_started_at_fence_stale_results():
    work = running()
    start = now()
    assert balances.save(
        work, start, data={"amount_minor": 12, "currency": "GBP", "basis": "available"}
    )
    assert not balances.save(work, start - timedelta(seconds=1), data={"amount_minor": 99})
    assert not balances.save(work, start, outcome="error")
    with session() as s, s.begin():
        s.get(Job, work["id"]).lease_token = "new-lease"
    assert not balances.save(work, start + timedelta(seconds=1), data={"amount_minor": 99})
    assert balances.report()["items"][0]["data"]["amount_minor"] == 12
    assert len(balances.report()["items"][0]["history"]) == 1


def test_expired_lease_or_disabled_account_does_not_fetch(monkeypatch):
    work = running()
    monkeypatch.setattr(monzo, "bank_get", lambda *args: pytest.fail("must not fetch"))
    with session() as s, s.begin():
        s.get(Job, work["id"]).lease_until = now() - timedelta(seconds=1)
    balances.collect(work)
    with session() as s, s.begin():
        s.get(Job, work["id"]).lease_until = now() + timedelta(minutes=5)
        s.get(Account, "acc_test").enabled = False
    balances.collect(work)


def test_daily_snapshots_use_london_day_last_success_and_leave_gaps(monkeypatch):
    work = running()
    # London midnight is 23:00 UTC before the autumn clock change.
    moment = datetime(2026, 10, 24, 22, 59, tzinfo=UTC)
    monkeypatch.setattr(balances, "now", lambda: moment)
    with session() as s, s.begin():
        s.get(Job, work["id"]).lease_until = moment + timedelta(days=5)
    balances.save(work, moment, data={"amount_minor": 1, "currency": "GBP", "basis": "available"})
    moment += timedelta(minutes=2)
    balances.save(work, moment, data={"amount_minor": 2, "currency": "GBP", "basis": "available"})
    moment += timedelta(minutes=1)
    balances.save(work, moment, outcome="error")
    moment += timedelta(days=2)
    balances.save(work, moment, data={"amount_minor": 3, "currency": "GBP", "basis": "available"})
    with session() as s:
        rows = s.scalars(select(BalanceSnapshot).order_by(BalanceSnapshot.day)).all()
        assert [str(x.day) for x in rows] == ["2026-10-24", "2026-10-25", "2026-10-26"]
        assert [x.data["amount_minor"] for x in rows] == [1, 2, 3]
    # Following day skipped entirely, then another successful observation.
    moment += timedelta(days=2)
    balances.save(work, moment, data={"amount_minor": 4, "currency": "GBP", "basis": "available"})
    assert "2026-10-27" not in [str(x["day"]) for x in balances.report()["items"][0]["history"]]


def test_report_owner_only_private_no_bank_calls(
    client, owner_headers, automation_headers, monkeypatch
):
    monkeypatch.setattr(monzo, "bank_get", lambda *a: pytest.fail("reads must use DB only"))
    assert client.get("/v1/dashboard/balances").status_code == 401
    assert client.get("/v1/dashboard/balances", headers=automation_headers).status_code == 403
    result = client.get("/v1/dashboard/balances", headers=owner_headers)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert all(x["state"] == "unavailable" and x["data"] is None for x in result.json()["items"])
    assert client.post("/v1/dashboard/balances", headers=owner_headers).status_code == 405


def test_freshness_and_unsupported_preserve_history_but_hide_current(monkeypatch):
    work = running()
    start = now()
    balances.save(work, start, data={"amount_minor": 8, "currency": "GBP", "basis": "available"})
    monkeypatch.setattr(balances, "now", lambda: start + timedelta(minutes=46))
    assert balances.report()["items"][0]["state"] == "stale"
    monkeypatch.setattr(balances, "now", now)
    balances.save(work, now(), outcome="unsupported")
    report = balances.report()["items"][0]
    assert report["state"] == "unsupported" and report["data"] is None
    assert len(report["history"]) == 1
    with session() as s, s.begin():
        s.get(Job, work["id"]).state = "failed"
        assert work["id"] not in balances.schedule(s, "acc_test")


def test_worker_failure_retry_does_not_block_transaction_ingestion(monkeypatch, payment):
    with session() as s, s.begin():
        balances.schedule(s, "acc_test")

    def reply(path, params):
        if path == "/balance":
            raise ServiceError("monzo_http_429", True, 120)
        if path == "/pots":
            return {"pots": []}
        return {"transaction": payment}

    monkeypatch.setattr(monzo, "bank_get", reply)
    for _ in range(2):
        assert tick("sync")
    monzo.execute(
        {
            "kind": "fetch_transaction",
            "payload": {"account_id": "acc_test", "transaction_id": payment["id"]},
        }
    )
    with session() as s:
        from monzo_backend.models import Transaction

        assert s.get(Transaction, payment["id"]) is not None
        job = s.scalar(
            select(Job).where(
                Job.kind == "collect_balance", Job.payload["source"].as_string() == "current"
            )
        )
        assert job.state == "pending" and job.available_at > now() + timedelta(seconds=100)
        assert (
            s.scalar(select(func.count()).select_from(Job).where(Job.kind == "collect_balance"))
            == 2
        )


def test_reconciliation_queues_balances_without_changing_sync_contract():
    with session() as s, s.begin():
        sync_ids = monzo.schedule_sync(s)
        assert len(sync_ids) == 1
        assert (
            s.scalar(select(func.count()).select_from(Job).where(Job.kind == "collect_balance"))
            == 2
        )


def test_concurrent_schedules_keep_one_active_job_per_source():
    from concurrent.futures import ThreadPoolExecutor

    def enqueue_once(_):
        with session() as s, s.begin():
            return balances.schedule(s, "acc_test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue_once, range(2)))
    assert results[0] == results[1]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 2


def test_retry_exhaustion_preserves_observation_and_transaction_sync(monkeypatch):
    from monzo_backend.config import settings

    monkeypatch.setenv("MONZO_MAX_ATTEMPTS", "1")
    settings.cache_clear()
    with session() as s, s.begin():
        balances.schedule(s, "acc_test")

    def fail(*args):
        raise ServiceError("monzo_http_503", True)

    monkeypatch.setattr(monzo, "bank_get", fail)
    assert tick("sync")
    assert tick("sync")
    with session() as s:
        assert all(j.state == "failed" for j in s.scalars(select(Job)))
        assert s.get(Account, "acc_test").history_status == "not_started"
        assert all(
            x.outcome == "error" and x.data is None for x in s.scalars(select(BalanceObservation))
        )
    assert not tick("sync")


@pytest.mark.parametrize(
    "instant,expected",
    [
        (datetime(2026, 1, 31, 23, 59, tzinfo=UTC), "2026-01-31"),
        (datetime(2026, 2, 1, 0, 1, tzinfo=UTC), "2026-02-01"),
        (datetime(2026, 3, 29, 23, 1, tzinfo=UTC), "2026-03-30"),
    ],
)
def test_snapshot_month_and_spring_dst_boundaries(monkeypatch, instant, expected):
    work = running()
    monkeypatch.setattr(balances, "now", lambda: instant)
    balances.save(work, instant, data={"amount_minor": 0, "currency": "GBP", "basis": "available"})
    with session() as s:
        assert str(s.scalar(select(BalanceSnapshot)).day) == expected
