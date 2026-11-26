from datetime import timedelta

import pytest
from sqlalchemy import select

from monzo_backend.backfill_payment_groups import backfill
from monzo_backend.classifier import cache_keys
from monzo_backend.config import settings
from monzo_backend.db import engine, session
from monzo_backend.domain import ingest
from monzo_backend.mcp.cursors import Cursors
from monzo_backend.mcp.server import execute
from monzo_backend.models import MerchantIdentity, PaymentGroupMatcher, Rule, Transaction, now


def seed(payment):
    data = {}
    for tid, amount, name in [
        ("extra1", -300, "Monzo Extra"),
        ("extra2", -300, "Monzo Extra"),
        ("out", -2300, "Just Eat UK"),
        ("in1", 1000, "Just Eat.co.uk Limited"),
        ("in2", 2000, "Just Eat.co.uk Limited"),
    ]:
        data[tid] = {
            **payment,
            "id": tid,
            "merchant": None,
            "amount": amount,
            "settled": payment["created"],
            "scheme": "faster_payments",
            "counterparty": {"name": name, "sort_code": "12-34-56", "account_number": "12345678"},
        }
        if tid.startswith("extra"):
            data[tid].update(
                counterparty=None,
                scheme="monzo_paid",
                description="Extra",
                metadata={"plan_definition_id": "extra-plan"},
            )
    rows = []
    with session() as s, s.begin():
        for tid, d in data.items():
            tx = ingest(s, "acc_test", d, allow_ai=False)
            rows.append(
                {
                    "id": tid,
                    "expected_version": tx.version,
                    "name": tx.payment_name,
                    "amount": tx.amount,
                    "currency": tx.currency,
                    "group": "Monzo Extra" if tid.startswith("extra") else "Just Eat",
                }
            )
    return data, {"batch_id": "test-business-groups", "payments": rows}


def test_backfill_atomic_and_directional_review(payment, client, owner_headers):
    data, manifest = seed(payment)
    assert backfill(manifest, fetch=data.get)["selected"] == 5
    with pytest.raises(ValueError):
        backfill(manifest, apply=True, fetch=lambda tid: {**data[tid], "amount": -1})
    with session() as s:
        assert not s.scalars(select(PaymentGroupMatcher)).all()
    backfill(manifest, apply=True, fetch=data.get)
    assert backfill(manifest, apply=True, fetch=lambda _: pytest.fail("replay must not fetch"))[
        "already_applied"
    ]
    r = client.get(
        "/v1/dashboard/review-merchants", params={"tab": "needs_review"}, headers=owner_headers
    )
    assert r.status_code == 200, r.text
    assert {x["name"]: x["payment_count"] for x in r.json()["items"]} == {
        "Monzo Extra · Money out": 2,
        "Just Eat · Money out": 1,
        "Just Eat · Money in": 2,
    }
    with session() as s, s.begin():
        for tid, d in data.items():
            tx = s.get(Transaction, tid)
            before = (tx.version, tx.category, tx.category_source, tx.payment_group_identity)
            assert tx.merchant_id is None
            ingest(s, "acc_test", d, allow_ai=False)
            assert (
                tx.version,
                tx.category,
                tx.category_source,
                tx.payment_group_identity,
            ) == before


def test_directional_category_batch_future_rules_and_undo(payment, client, owner_headers):
    data, manifest = seed(payment)
    backfill(manifest, apply=True, fetch=data.get)

    def apply(tid, category):
        with session() as s:
            version = s.get(Transaction, tid).version
        body = {
            "category": category,
            "scope": "unverified",
            "future_rule": True,
            "group_merchant": True,
            "expected_version": version,
        }
        p = client.post(
            f"/v1/dashboard/transactions/{tid}/category-preview", json=body, headers=owner_headers
        )
        assert p.status_code == 200, p.text
        result = client.put(
            f"/v1/dashboard/transactions/{tid}/category-change",
            json={**body, "preview_token": p.json()["preview_token"]},
            headers=owner_headers,
        )
        assert result.status_code == 200, result.text
        return p.json(), result.json()

    p, out_result = apply("out", "eating_out")
    assert p["matched_count"] == 1
    p, _ = apply("in1", "income")
    assert p["matched_count"] == 2
    with session() as s, s.begin():
        assert s.get(Transaction, "out").category == "eating_out"
        assert s.get(Transaction, "in1").category == "income"
        assert s.get(Transaction, "in2").category == "income"
        rules = s.scalars(select(Rule)).all()
        assert len(rules) == 2
        assert all(r.group_id is None for r in rules)
        for tid, category in [("out", "eating_out"), ("in1", "income")]:
            d = {
                **data[tid],
                "id": "future-" + tid,
                "created": (now() + timedelta(seconds=1)).isoformat(),
            }
            tx = ingest(s, "acc_test", d, allow_ai=False)
            assert tx.category == category
        a = s.get(Transaction, "out")
        b = s.get(Transaction, "in1")
        assert cache_keys(s, a, settings())[0] != cache_keys(s, b, settings())[0]
    # The same saved group is visible in reporting, without conflating category rules.
    cursors = Cursors("payment-group-test-" + "x" * 32, "issuer|owner")
    a = execute("get_transaction", {"transaction_id": "out"}, engine(), cursors)["payment"]
    b = execute("get_transaction", {"transaction_id": "in1"}, engine(), cursors)["payment"]
    assert a["merchant_group_id"] == b["merchant_group_id"]
    assert a["category_identity_id"] != b["category_identity_id"]
    assert a["merchant_id"] is b["merchant_id"] is None
    assert a["directional_group"] and b["directional_group"]
    undo = client.post(
        "/v1/dashboard/category-changes/" + out_result["batch_id"] + "/undo", headers=owner_headers
    )
    assert undo.status_code == 200, undo.text
    with session() as s:
        assert s.get(Transaction, "in1").category == "income"
        assert s.get(Transaction, "out").category is None
        assert len(s.scalars(select(Rule)).all()) == 1


def test_future_matching_rejects_lookalikes_changed_bank_and_plan(payment):
    data, manifest = seed(payment)
    backfill(manifest, apply=True, fetch=data.get)
    with session() as s, s.begin():
        for index, (tid, patch) in enumerate(
            [
                ("extra1", {"metadata": {"plan_definition_id": "different"}}),
                ("out", {"counterparty": {"name": "Just Eat UK Other"}}),
                (
                    "out",
                    {"counterparty": {**data["out"]["counterparty"], "account_number": "87654321"}},
                ),
                ("out", {"amount": 1000}),
            ]
        ):
            tx = ingest(
                s, "acc_test", {**data[tid], **patch, "id": "new" + str(index)}, allow_ai=False
            )
            assert tx.payment_group_identity is None
        new = ingest(
            s, "acc_test", {**data["extra1"], "id": "new-extra", "amount": -400}, allow_ai=False
        )
        assert new.payment_group_identity
        tx = s.get(Transaction, "out")
        tx.category = "gifts"
        tx.category_source = "manual"
        tx.category_status = "assigned"
        ingest(s, "acc_test", data["out"])
        assert tx.category == "gifts"


def test_scoped_rule_does_not_hide_behind_group_wide_rule(payment):
    data, manifest = seed(payment)
    backfill(manifest, apply=True, fetch=data.get)
    with session() as s, s.begin():
        tx = s.get(Transaction, "out")
        group = s.get(MerchantIdentity, tx.payment_group_identity).group_id
        s.add(Rule(group_id=group, category="shopping"))
        future = ingest(s, "acc_test", {**data["in1"], "id": "future-in"}, allow_ai=False)
        assert future.category is None


def test_backfill_requeues_pending_classification(payment):
    from monzo_backend.classifier import classify
    from monzo_backend.domain import claim, finish

    data = {
        **payment,
        "merchant": None,
        "counterparty": {"name": "Example Business"},
        "scheme": "faster_payments",
    }
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", data)
        manifest = {
            "batch_id": "pending-group",
            "payments": [
                {
                    "id": tx.id,
                    "expected_version": tx.version,
                    "name": tx.payment_name,
                    "amount": tx.amount,
                    "currency": tx.currency,
                    "group": "Example",
                }
            ],
        }
        old_work = claim(s, "category")
    backfill(manifest, apply=True, fetch=lambda _: data)
    with session() as s, s.begin():
        finish(s, old_work, result=classify(old_work))
        work = claim(s, "category")
        assert work["payload"]["version"] == s.get(Transaction, tx.id).version
    result = classify(work)
    with session() as s, s.begin():
        finish(s, work, result=result)
        assert s.get(Transaction, tx.id).category_status == "needs_review"
        assert claim(s, "category") is None
