from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from monzo_backend import budgets
from monzo_backend.api import app
from monzo_backend.browser_auth import hash_password
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import (
    Account,
    Audit,
    BudgetVersion,
    Connection,
    Job,
    ReportingCoverage,
    Transaction,
)

PERIOD = "2026-10"
INSTANT = datetime(2026, 10, 24, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(budgets, "now", lambda: INSTANT)


def edit(**extra):
    return budgets.BudgetEdit(period=PERIOD, amount_minor=5000, expected_version=0, **extra)


def read(client, headers, **extra):
    response = client.get(
        "/v1/dashboard/budgets", params={"period": PERIOD, **extra}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def add(s, payment, txid, amount, **extra):
    data = {
        **payment,
        "id": txid,
        "amount": amount,
        "settled": payment["created"],
        "scheme": "mastercard",
        **extra,
    }
    return ingest(s, data["account_id"], data)


def test_versions_zero_removal_noop_and_immutable_history(client, owner_headers):
    initial = read(client, owner_headers)
    assert initial["items"][0]["target_minor"] is None
    assert initial["items"][0]["remaining_minor"] is None
    assert budgets.save(edit()) == {"version": 1, "changed": True}
    assert (
        budgets.save(budgets.BudgetEdit(period=PERIOD, amount_minor=0, expected_version=1))[
            "version"
        ]
        == 2
    )
    zero = read(client, owner_headers)
    assert zero["revision"] > initial["revision"]
    assert zero["items"][0]["remaining_minor"] == 0
    assert budgets.save(budgets.BudgetEdit(period=PERIOD, amount_minor=0, expected_version=2)) == {
        "version": 2,
        "changed": False,
    }
    assert read(client, owner_headers)["revision"] == zero["revision"]
    budgets.save(budgets.BudgetEdit(period=PERIOD, amount_minor=None, expected_version=2))
    removed = read(client, owner_headers)["items"][0]
    assert removed["target_minor"] is None
    assert [v["amount_minor"] for v in removed["history"]] == [None, 0, 5000]
    with session() as s:
        assert (
            s.scalar(select(func.count()).select_from(Audit).where(Audit.action == "budget_saved"))
            == 3
        )
        assert s.scalar(select(func.count()).select_from(Job)) == 0
    for sql in ("UPDATE budget_versions SET amount_minor=5", "DELETE FROM budget_versions"):
        with pytest.raises(DBAPIError, match="append-only"), session() as s, s.begin():
            s.execute(text(sql))


def test_current_future_edits_do_not_change_past_targets_or_roll_forward(monkeypatch):
    budgets.save(edit())
    monkeypatch.setattr(budgets, "now", lambda: datetime(2026, 11, 1, tzinfo=UTC))
    with pytest.raises(HTTPException) as error:
        budgets.save(budgets.BudgetEdit(period=PERIOD, amount_minor=1, expected_version=1))
    assert error.value.status_code == 409
    assert budgets.report(PERIOD)["items"][0]["target_minor"] == 5000
    assert budgets.report("2026-11")["items"][0]["target_minor"] is None
    budgets.save(budgets.BudgetEdit(period="2026-12", amount_minor=7000, expected_version=0))
    future = budgets.report("2026-12")
    assert future["coverage"]["state"] == "not_started"
    assert future["items"][0]["remaining_minor"] == 7000
    assert not budgets.report(PERIOD)["editable"]


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_creation_and_edits_have_one_winner(existing):
    if existing:
        budgets.save(edit())
    barrier = Barrier(2)

    def submit(amount):
        barrier.wait(timeout=5)
        try:
            return budgets.save(
                budgets.BudgetEdit(
                    period=PERIOD, amount_minor=amount, expected_version=int(existing)
                )
            )
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, [6000, 7000]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count(409) == 1
    assert len(budgets.report(PERIOD)["items"][0]["history"]) == 1 + int(existing)


@pytest.mark.parametrize(
    "patch",
    [
        {"amount_minor": True},
        {"amount_minor": "5"},
        {"amount_minor": 1.5},
        {"amount_minor": -1},
        {"amount_minor": 1000000000001},
        {"expected_version": -1},
        {"expected_version": True},
        {"currency": "USD"},
        {"category": "fake"},
        {"period": "2026-13"},
        {"period": "2026-1"},
        {"connection_id": 2},
    ],
)
def test_rejects_invalid_write_inputs(client, owner_headers, patch):
    body = {"period": PERIOD, "amount_minor": 5000, "expected_version": 0, **patch}
    assert client.put("/v1/dashboard/budgets", json=body, headers=owner_headers).status_code == 422
    assert budgets.report(PERIOD)["items"][0]["version"] == 0


def test_currency_and_account_validation_and_other_owner_isolation(client, owner_headers, payment):
    with session() as s, s.begin():
        s.add(Connection(id=2, user_id="someone-else", access_token="fake", expires_at=INSTANT))
        s.flush()
        s.add_all(
            [
                Account(id="other", connection_id=2, kind="uk_retail"),
                Account(id="rewards", connection_id=1, kind="uk_rewards"),
            ]
        )
        s.flush()
        add(s, payment, "other-tx", -999999, account_id="other")
        add(s, payment, "reward-tx", -888888, account_id="rewards")
        s.add(
            BudgetVersion(
                connection_id=2,
                account_scope="combined",
                month=datetime(2026, 10, 1).date(),
                category="",
                version=1,
                currency="GBP",
                amount_minor=123,
            )
        )
    for aid in ("other", "rewards", "missing"):
        assert (
            client.get(
                "/v1/dashboard/budgets",
                params={"period": PERIOD, "account_id": aid},
                headers=owner_headers,
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/v1/dashboard/budgets",
                json=edit(account_id=aid).model_dump(),
                headers=owner_headers,
            ).status_code
            == 404
        )
    assert (
        client.get(
            "/v1/dashboard/budgets",
            params={"period": PERIOD, "currency": "USD"},
            headers=owner_headers,
        ).status_code
        == 422
    )
    result = read(client, owner_headers)
    assert result["account_ids"] == ["acc_test"]
    assert result["items"][0]["target_minor"] is None
    assert result["items"][0]["spent_minor"] == 0


def test_owner_auth_csrf_no_cache(client, owner_headers, automation_headers, monkeypatch):
    path = "/v1/dashboard/budgets"
    for headers, code in (({}, 401), (automation_headers, 403)):
        assert client.get(path, params={"period": PERIOD}, headers=headers).status_code == code
        assert client.put(path, json=edit().model_dump(), headers=headers).status_code == code
    origin = "https://finance.example.test"
    monkeypatch.setenv("MONZO_DASHBOARD_ORIGIN", origin)
    monkeypatch.setenv("MONZO_DASHBOARD_PASSWORD_HASH", hash_password("test-password"))
    settings.cache_clear()
    headers = {"Origin": origin, "X-Dashboard-Request": "1"}
    with TestClient(app, base_url=origin) as browser:
        assert (
            browser.post(
                "/dashboard/login", headers=headers, json={"password": "test-password"}
            ).status_code
            == 200
        )
        for bad in ({}, {"Origin": origin}, {**headers, "Origin": "https://evil.test"}):
            assert browser.put(path, json=edit().model_dump(), headers=bad).status_code == 403
        assert browser.put(path, json=edit().model_dump(), headers=headers).status_code == 200
        response = browser.get(path, params={"period": PERIOD})
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["items"][0]["target_minor"] == 5000


def test_spending_parity_flex_pots_refunds_pending_categories_and_scope(
    client, owner_headers, payment
):
    with session() as s, s.begin():
        s.add(Account(id="flex", connection_id=1, kind="uk_monzo_flex"))
        s.flush()
        shop = add(s, payment, "shop", -1000)
        shop.category = "groceries"
        shop.category_status = "assigned"
        add(s, payment, "flex-shop", -3000, account_id="flex")
        metadata = {
            "ledger_insertion_id": "pair",
            "monzo_flex_id": "facility",
            "flex_transaction_type": "repayment",
        }
        add(s, payment, "repay-out", -2000, scheme="monzo_flex", metadata=metadata)
        add(s, payment, "repay-in", 2000, account_id="flex", scheme="monzo_flex", metadata=metadata)
        add(
            s,
            payment,
            "pot",
            -500,
            scheme="uk_retail_pot",
            metadata={"pot_id": "pot", "pot_account_id": "own"},
        )
        add(s, payment, "refund", 100)
        add(s, payment, "pending", -900, settled="")
        add(s, payment, "declined", -950, decline_reason="OTHER")
        add(s, payment, "usd", -800, currency="USD")
        add(s, payment, "unmatched", -200, scheme="monzo_flex")
        missing = add(s, payment, "missing-facts", -300)
        missing.reporting_facts = None
    budgets.save(edit())
    budgets.save(edit(category="groceries"))
    before = read(client, owner_headers)
    overall = before["items"][0]
    assert (
        overall["spent_minor"],
        overall["remaining_minor"],
        overall["pending_minor"],
        overall["credits_minor"],
    ) == (4500, 500, 900, 100)
    assert before["unmatched_flex_count"] == before["evidence_missing_count"] == 1
    for aid in ("combined", "acc_test", "flex"):
        budget = read(client, owner_headers, account_id=aid)
        spending = client.get(
            "/v1/dashboard/report",
            params={"period": PERIOD, "account_id": aid},
            headers=owner_headers,
        ).json()
        assert budget["items"][0]["spent_minor"] == spending["current"]["debits_minor"]
        assert budget["items"][0]["credits_minor"] == spending["current"]["credits_minor"]
        assert {
            i["category"]: i["spent_minor"] for i in budget["items"][1:] if i["spent_minor"]
        } == {i["key"]: i["amount_minor"] for i in spending["categories"]}
    with session() as s, s.begin():
        s.get(Transaction, "pending").settled_at = INSTANT
        s.get(Transaction, "shop").category = "transport"
    after = read(client, owner_headers)
    assert after["revision"] > before["revision"]
    assert after["items"][0]["spent_minor"] == 5400
    assert after["items"][0]["remaining_minor"] == -400
    assert after["items"][0]["pending_minor"] == 0
    groceries = next(i for i in after["items"] if i["category"] == "groceries")
    assert (groceries["spent_minor"], groceries["target_minor"], groceries["version"]) == (
        0,
        5000,
        1,
    )
    repeated = read(client, owner_headers)
    assert repeated == after


@pytest.mark.parametrize(
    "period,start,end",
    [
        ("2026-03", "2026-03-01T00:00:00+00:00", "2026-03-31T23:00:00+00:00"),
        ("2026-10", "2026-09-30T23:00:00+00:00", "2026-11-01T00:00:00+00:00"),
        ("2026-12", "2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
    ],
)
def test_month_boundaries_dst_and_year_rollover(payment, period, start, end):
    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
    with session() as s, s.begin():
        for i, stamp in enumerate(
            (start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end)
        ):
            add(s, payment, f"boundary-{i}", -100, created=stamp.isoformat())
    result = budgets.report(period)
    assert result["start"] == start and result["end"] == end
    assert result["items"][0]["spent_minor"] == 200


def test_coverage_merges_adjacent_intervals_requires_every_account_and_preserves_gaps():
    start = budgets.bounds(PERIOD, "month")[0]
    middle = start + timedelta(days=10)

    def window(aid, a, b):
        return ReportingCoverage(
            account_id=aid, start_at=a, end_at=b, source="test", observed_at=INSTANT
        )

    with session() as s, s.begin():
        s.add(Account(id="flex", connection_id=1, kind="uk_monzo_flex"))
        s.flush()
        s.add_all([window("acc_test", start, middle), window("acc_test", middle, INSTANT)])
    assert budgets.report(PERIOD)["coverage"]["state"] == "incomplete"
    assert budgets.report(PERIOD, "acc_test")["coverage"]["state"] == "complete"
    with session() as s, s.begin():
        s.add_all(
            [
                window("flex", start, middle),
                window("flex", middle + timedelta(microseconds=1), INSTANT),
            ]
        )
    assert budgets.report(PERIOD)["coverage"]["state"] == "incomplete"
    with session() as s, s.begin():
        s.add(window("flex", middle, middle + timedelta(microseconds=1)))
    assert budgets.report(PERIOD)["coverage"]["state"] == "complete"
