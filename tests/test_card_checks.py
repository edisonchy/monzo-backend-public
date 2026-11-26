import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from monzo_backend.card_checks import confirm, is_card_check
from monzo_backend.classifier import classify
from monzo_backend.db import session
from monzo_backend.domain import claim, finish, ingest
from monzo_backend.models import Audit, Job, Rule, Transaction

BASE = "/v1/dashboard/transactions/tx_test/"
SCOPE = {"account_id": "combined", "period": "2026-10", "currency": "GBP"}


def seed(payment):
    data = {**payment, "amount": 0}
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", data)
        tx.category_status, tx.category_source = "needs_review", "review"
    return data


def mark(client, owner_headers, version=1):
    return client.put(
        BASE + "card-verification", headers=owner_headers, json={"expected_version": version}
    )


def test_owner_confirmation_is_scoped_audited_idempotent_and_authenticated(
    client,
    owner_headers,
    automation_headers,
    payment,
):
    seed(payment)
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "id": "real_charge"})
        ingest(s, "acc_test", {**payment, "id": "other_zero", "amount": 0})
    for headers in ({}, automation_headers):
        assert client.put(
            BASE + "card-verification", headers=headers, json={"expected_version": 1}
        ).status_code in (401, 403)
    assert mark(client, owner_headers, 99).status_code == 409
    result = mark(client, owner_headers)
    assert result.status_code == 200, result.text
    assert mark(client, owner_headers, 2).json() == result.json()
    with session() as s:
        tx = s.get(Transaction, "tx_test")
        assert is_card_check(tx) and tx.version == 2
        assert (tx.category, tx.category_source, tx.category_status) == (
            None,
            "owner_verification",
            "excluded",
        )
        assert not is_card_check(s.get(Transaction, "real_charge"))
        assert not is_card_check(s.get(Transaction, "other_zero"))
        audit = s.scalar(select(Audit).where(Audit.action == "card_verification_confirmed"))
        assert audit.actor == "owner" and audit.detail["previous"]["status"] == "needs_review"
        assert s.scalar(select(func.count()).select_from(Audit)) == 1
        assert s.scalar(select(Rule)) is None


@pytest.mark.parametrize(
    "changes", [{"amount": -100}, {"amount": 100}, {"decline_reason": "OTHER"}]
)
def test_nonzero_or_declined_cannot_be_confirmed(client, owner_headers, payment, changes):
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "amount": 0, **changes})
    assert mark(client, owner_headers).status_code == 422
    with session() as s:
        assert not is_card_check(s.get(Transaction, "tx_test"))


@pytest.mark.parametrize("settled", [False, True])
def test_history_kept_but_reports_counts_review_and_reclassification_exclude_check(
    client,
    owner_headers,
    payment,
    settled,
):
    data = seed({**payment, "settled": payment["created"] if settled else ""})
    assert mark(client, owner_headers).status_code == 200
    with session() as s, s.begin():
        ordinary = ingest(s, "acc_test", {**data, "id": "ordinary_zero"})
        ordinary.category_status = "needs_review"
        charge = ingest(s, "acc_test", {**payment, "id": "charge", "settled": payment["created"]})
        charge.category, charge.category_source, charge.category_status = (
            "bills",
            "manual",
            "assigned",
        )
    for account in ("combined", "acc_test"):
        params = {**SCOPE, "account_id": account}
        report = client.get("/v1/dashboard/report", params=params, headers=owner_headers).json()
        assert report["current"]["debits_minor"] == 1234
        assert report["current"]["count"] == 1 + int(settled)
        assert report["review_count"] == 1
        assert report["current"]["excluded_count"] == 0  # Not a transfer.
        rows = client.get("/v1/dashboard/transactions", params=params, headers=owner_headers).json()
        check = next(t for t in rows["items"] if t["id"] == "tx_test")
        assert check["card_verification"] and check["category_status"] == "excluded"
        assert bool(check["settled_at"]) == settled
        for extra in ({"status": "needs_review"}, {"spending_only": True}):
            visible = client.get(
                "/v1/dashboard/transactions", params={**params, **extra}, headers=owner_headers
            ).json()["items"]
            assert all(t["id"] != "tx_test" for t in visible)
    cashflow = client.get(
        "/v1/cashflow",
        headers=owner_headers,
        params={
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-11-01T00:00:00Z",
            "settled_only": False,
        },
    ).json()
    assert sum(g["count"] for g in cashflow["groups"]) == 2
    assert client.post("/v1/reclassify", headers=owner_headers).json()["queued"] == 1


def test_no_provider_for_card_check_and_old_worker_cannot_overwrite(
    client,
    owner_headers,
    payment,
    monkeypatch,
):
    seed(payment)
    with session() as s, s.begin():
        work = claim(s, "category")
    assert mark(client, owner_headers).status_code == 200

    def unexpected(*args, **kwargs):
        pytest.fail("Card checks must not request AI or web research")

    monkeypatch.setattr("monzo_backend.classifier.request", unexpected)
    monkeypatch.setattr("monzo_backend.classifier.research", unexpected)
    assert classify(work) == {"skipped": "stale_input"}
    with session() as s, s.begin():
        finish(s, work, {"category": "shopping", "source": "ai", "status": "needs_review"})
        tx = s.get(Transaction, "tx_test")
        assert tx.category_status == "excluded" and tx.category is None and tx.version == 2


def test_repeated_zero_sync_preserves_confirmation_without_new_jobs(client, owner_headers, payment):
    data = seed(payment)
    mark(client, owner_headers)
    with session() as s, s.begin():
        before_jobs = s.scalar(select(func.count()).select_from(Job))
        tx = ingest(s, "acc_test", data)
        assert tx.version == 2
        ingest(s, "acc_test", {**data, "settled": payment["created"], "category": "shopping"})
        assert is_card_check(tx) and tx.category_status == "excluded" and tx.category is None
        assert s.scalar(select(func.count()).select_from(Job)) == before_jobs


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": -2000},
        {"currency": "EUR"},
        {"merchant": {"id": "new_id", "name": "New merchant"}},
        {"decline_reason": "OTHER"},
    ],
)
def test_changed_bank_payment_revokes_check_and_requeues(client, owner_headers, payment, changes):
    data = seed(payment)
    mark(client, owner_headers)
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", {**data, **changes, "settled": payment["created"]})
        assert not is_card_check(tx) and tx.transaction_type == "unknown"
        assert tx.version == 3 and tx.category_status == "pending" and tx.category_source is None
        assert s.scalar(select(Job).where(Job.dedupe_key == "category:tx_test:3:v1"))
        assert s.scalar(select(Audit).where(Audit.action == "card_verification_revoked"))
    if changes.get("amount"):
        report = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
        assert report["current"]["debits_minor"] == 2000 and report["current"]["count"] == 1


def test_category_edits_reject_checks_and_bulk_skips_them(client, owner_headers, payment):
    seed(payment)
    mark(client, owner_headers)
    choice = {"category": "bills", "scope": "all", "expected_version": 2, "future_rule": True}
    assert (
        client.post(BASE + "category-preview", headers=owner_headers, json=choice).status_code
        == 409
    )
    for path in (BASE + "approve", "/v1/transactions/tx_test/category"):
        assert (
            client.put(
                path,
                headers=owner_headers,
                json={
                    "category": "bills",
                    "expected_version": 2,
                },
            ).status_code
            == 409
        )
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "id": "charge"})
    path = "/v1/dashboard/transactions/charge/"
    choice["expected_version"] = 1
    preview = client.post(path + "category-preview", headers=owner_headers, json=choice).json()
    assert preview["card_check_count"] == 1 and preview["change_count"] == 1
    result = client.put(
        path + "category-change",
        headers=owner_headers,
        json={
            **choice,
            "preview_token": preview["preview_token"],
        },
    )
    assert result.status_code == 200, result.text
    with session() as s:
        assert is_card_check(s.get(Transaction, "tx_test"))
        assert s.get(Transaction, "tx_test").category_status == "excluded"
        assert s.get(Transaction, "charge").category == "bills"


def test_confirmation_invalidates_prior_category_preview(client, owner_headers, payment):
    seed(payment)
    choice = {"category": "bills", "scope": "this", "expected_version": 1}
    preview = client.post(BASE + "category-preview", headers=owner_headers, json=choice).json()
    mark(client, owner_headers)
    assert (
        client.put(
            BASE + "category-change",
            headers=owner_headers,
            json={
                **choice,
                "preview_token": preview["preview_token"],
            },
        ).status_code
        == 409
    )


def test_nonzero_with_stale_marker_is_never_excluded(client, owner_headers, payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", {**payment, "settled": payment["created"]})
        tx.transaction_type = "card_verification"
        tx.category_status = "needs_review"
    report = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
    assert report["current"]["debits_minor"] == 1234 and report["review_count"] == 1
    with session() as s, s.begin():
        with pytest.raises(HTTPException) as error:
            confirm(s, "tx_test", 1)
        assert error.value.status_code == 422
