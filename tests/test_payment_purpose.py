import pytest
from sqlalchemy import select

from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import PaymentPurposeRule
from monzo_backend.payment_purpose import confirm, confirmed_purpose, counterparty_key, revoke_rule
from monzo_backend.spending import exclusions


def bank(payment, **kw):
    return {
        **payment,
        "merchant": None,
        "settled": payment["created"],
        "counterparty": {"name": "A Person", "sort_code": "12-34-56", "account_number": "12345678"},
        **kw,
    }


def test_exact_identity_memory_direction_and_manual_priority(payment):
    with session() as s, s.begin():
        data = bank(payment)
        tx = ingest(s, "acc_test", data, allow_ai=False)
        confirm(s, tx, "rent", expected_version=tx.version, future=True)
        version = tx.version
        ingest(s, "acc_test", data)
        assert tx.version == version
        next_tx = ingest(s, "acc_test", {**data, "id": "next", "amount": -10000})
        assert next_tx.category == "bills"
        assert confirmed_purpose(next_tx)["label"] == "Rent"
        renamed = ingest(
            s,
            "acc_test",
            {
                **data,
                "id": "renamed",
                "counterparty": {**data["counterparty"], "name": "Different spelling"},
            },
        )
        assert confirmed_purpose(renamed)["purpose"] == "rent"
        other = ingest(
            s,
            "acc_test",
            {
                **data,
                "id": "other",
                "counterparty": {**data["counterparty"], "account_number": "87654321"},
            },
            allow_ai=False,
        )
        assert confirmed_purpose(other) is None
        incoming = ingest(
            s, "acc_test", {**data, "id": "incoming", "amount": 10000}, allow_ai=False
        )
        assert confirmed_purpose(incoming) is None
        next_tx.category, next_tx.category_source = "gifts", "manual"
        ingest(s, "acc_test", {**data, "id": "next", "amount": -10000})
        assert next_tx.category == "gifts"
        assert confirmed_purpose(next_tx) is None
        ingest(s, "acc_test", {**data, "id": "next", "amount": -11000})
        assert next_tx.category == "gifts"  # A later explicit edit survives bank refresh.
        rule = s.scalar(select(PaymentPurposeRule))
        revoke_rule(s, rule.id)
        assert renamed.category_status == "needs_review"
        assert (
            confirmed_purpose(tx)["purpose"] == "rent"
        )  # Explicit historical confirmation survives.


@pytest.mark.parametrize("purpose", ["car_purchase", "holiday"])
def test_one_off_never_generalises(payment, purpose):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment), allow_ai=False)
        with pytest.raises(ValueError):
            confirm(s, tx, purpose, expected_version=tx.version, future=True)
        confirm(s, tx, purpose, expected_version=tx.version)
        future = ingest(s, "acc_test", bank(payment, id="future"), allow_ai=False)
        assert confirmed_purpose(future) is None
        assert confirmed_purpose(tx)["scope"] == "payment"


def test_own_accounts_exclusion_and_changed_evidence(payment, client, owner_headers):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment), allow_ai=False)
        confirm(s, tx, "own_account", expected_version=tx.version, future=True)
        s.flush()
        assert exclusions(s)[tx.id]["reason"] == "own_account_transfer"
    # List endpoint provides the same evidence without bank fingerprints.
    result = client.get(
        "/v1/dashboard/transactions",
        params={"account_id": "combined", "period": "2026-10", "currency": "GBP"},
        headers=owner_headers,
    )
    assert result.status_code == 200
    assert "counterparty_key" not in result.text
    from monzo_backend.db import engine
    from monzo_backend.mcp.cursors import Cursors
    from monzo_backend.mcp.server import execute

    detail = execute(
        "get_transaction",
        {"transaction_id": "tx_test"},
        engine(),
        Cursors("purpose-test-" + "x" * 32, "issuer|owner"),
    )
    assert detail["payment"]["purpose_confirmation"]["purpose"] == "own_account"
    assert detail["payment"]["exclusion_reason"] == "own_account_transfer"
    with session() as s, s.begin():
        tx = ingest(
            s,
            "acc_test",
            bank(
                payment,
                counterparty={
                    "name": "A Person",
                    "sort_code": "12-34-56",
                    "account_number": "87654321",
                },
            ),
            allow_ai=False,
        )
        assert confirmed_purpose(tx) is None
        assert tx.id not in exclusions(s)


def test_missing_identifiers_no_name_rules(payment):
    assert counterparty_key({"counterparty": {"name": "A Person", "user_id": "user123"}}) is None
    assert (
        counterparty_key({"counterparty": {"sort_code": "000000", "account_number": "00000000"}})
        is None
    )
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment, counterparty={"name": "A Person"}), allow_ai=False)
        with pytest.raises(ValueError):
            confirm(s, tx, "own_account", expected_version=tx.version, future=True)
        confirm(s, tx, "own_account", expected_version=tx.version)
        s.flush()
        assert tx.id in exclusions(s)
        ingest(
            s,
            "acc_test",
            bank(payment, amount=-999, counterparty={"name": "A Person"}),
            allow_ai=False,
        )
        assert tx.id not in exclusions(s)


def test_atomic_manifest_and_idempotency(payment):
    from monzo_backend.backfill_payment_purpose import backfill

    data = bank(payment)
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", data, allow_ai=False)
        row = {
            "id": tx.id,
            "expected_version": tx.version,
            "amount": tx.amount,
            "currency": tx.currency,
            "counterparty_name": tx.counterparty_name,
            "purpose": "rent",
            "future": True,
        }
    manifest = {"batch_id": "test", "payments": [row]}
    assert not backfill(manifest, fetch=lambda _: data)["applied"]
    with pytest.raises(ValueError):
        backfill(manifest, apply=True, fetch=lambda _: {**data, "amount": -1})
    assert backfill(manifest, apply=True, fetch=lambda _: data)["applied"]
    assert backfill(manifest, apply=True, fetch=lambda _: pytest.fail("must not refetch"))[
        "already_applied"
    ]


@pytest.mark.parametrize("category", ["transfers", "gifts"])
def test_revoke_clears_rule_evidence_but_preserves_manual_category(
    payment, client, owner_headers, category
):
    from monzo_backend.models import Audit, Transaction

    with session() as s, s.begin():
        data = bank(payment)
        original = ingest(s, "acc_test", data, allow_ai=False)
        confirm(s, original, "own_account", expected_version=original.version, future=True)
        derived = ingest(s, "acc_test", {**data, "id": "derived"})
        untouched = ingest(s, "acc_test", {**data, "id": "untouched"})
        version = derived.version
        untouched_version = untouched.version
        rule_id = s.scalar(select(PaymentPurposeRule)).id
        assert derived.purpose_confirmation["rule_id"] == rule_id
    response = client.put(
        "/v1/transactions/derived/category",
        headers=owner_headers,
        json={"category": category, "expected_version": version},
    )
    assert response.status_code == 200, response.text
    with session() as s, s.begin():
        assert revoke_rule(s, rule_id)["payments_returned_to_review"] == 1
    with session() as s:
        derived = s.get(Transaction, "derived")
        assert derived.purpose_confirmation is None
        assert (derived.category, derived.category_source, derived.category_status) == (
            category,
            "manual",
            "assigned",
        )
        assert derived.version == version + 2
        untouched = s.get(Transaction, "untouched")
        assert untouched.category_status == "needs_review"
        assert untouched.purpose_confirmation is None
        assert untouched.version == untouched_version + 1
        audit = s.scalar(select(Audit).where(Audit.action == "payment_purpose_rule_revoked"))
        assert set(audit.detail["transaction_ids"]) == {"derived", "untouched"}
        assert "derived" not in exclusions(s)
        assert "untouched" not in exclusions(s)
        assert "tx_test" in exclusions(s)  # Direct owner confirmation survives rule revocation.
