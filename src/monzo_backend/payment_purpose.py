"""Owner knowledge, scoped to payments or exact bank counterparties; never fuzzy names."""

import hashlib
import hmac
import json
import re

from sqlalchemy import select

from monzo_backend.models import Audit, PaymentPurposeRule, now

PURPOSES = {
    "own_account": ("Own account transfer", "transfers"),
    "rent": ("Rent", "bills"),
    "car_purchase": ("Car purchase", "transport"),
    "holiday": ("Holiday", "holidays"),
}
REUSABLE = {"own_account", "rent"}


def counterparty_key(data):
    """Keyed fingerprint; do not persist account numbers, names or raw payloads.

    user_id is not a bank-account identity (and is often empty). Require both
    UK sort code and account number; absent identifiers cannot create a rule.
    """
    cp = data.get("counterparty") or {}
    if not isinstance(cp, dict):
        return None
    values = []
    for field, length in (("sort_code", 6), ("account_number", 8)):
        raw = cp.get(field)
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9 -]+", raw):
            return None
        value = re.sub(r"[ -]", "", raw)
        if len(value) != length or set(value) == {"0"}:
            return None
        values.append(value)
    from monzo_backend.config import settings

    return hmac.new(
        settings().encryption_key.get_secret_value().encode(),
        ("payment-counterparty-v1:" + ":".join(values)).encode(),
        hashlib.sha256,
    ).hexdigest()


def direction(tx):
    return "out" if tx.amount < 0 else "in"


def snapshot(tx):
    return {
        "account_id": tx.account_id,
        "amount": tx.amount,
        "currency": tx.currency,
        "counterparty_key": tx.counterparty_key,
    }


def confirmed_purpose(tx):
    saved = tx.purpose_confirmation or {}
    purpose = saved.get("purpose")
    if (
        purpose not in PURPOSES
        or saved.get("snapshot") != snapshot(tx)
        or tx.declined
        or tx.category != PURPOSES[purpose][1]
        or tx.category_source not in {"manual", "rule"}
        or tx.category_status != "assigned"
    ):
        return None
    # Keep bank identity fingerprints out of API responses.
    return {k: saved[k] for k in ("purpose", "label", "source", "scope", "confirmed_at")}


def assign(tx, purpose, *, rule_id=None, scope="payment"):
    tx.category = PURPOSES[purpose][1]
    tx.category_source = "rule" if rule_id else "manual"
    tx.category_status = "assigned"
    tx.version += 1
    tx.purpose_confirmation = {
        "purpose": purpose,
        "label": PURPOSES[purpose][0],
        "source": "owner_confirmed",
        "scope": scope,
        "rule_id": rule_id,
        "confirmed_at": now().isoformat(),
        "snapshot": snapshot(tx),
    }


def confirm(s, tx, purpose, *, expected_version, future=False):
    """Caller holds transaction lock; explicit owner confirmation, with an audit receipt."""
    if purpose not in PURPOSES or tx.version != expected_version or tx.declined or not tx.amount:
        raise ValueError("Invalid purpose or payment changed")
    if future and (purpose not in REUSABLE or not tx.counterparty_key):
        raise ValueError(
            "Reusable confirmation requires a stable bank identity and supported purpose"
        )
    if purpose == "rent" and tx.amount >= 0:
        raise ValueError("Rent confirmation requires an outgoing payment")
    rule = None
    if future:
        rule = s.scalar(
            select(PaymentPurposeRule)
            .where(
                PaymentPurposeRule.account_id == tx.account_id,
                PaymentPurposeRule.counterparty_key == tx.counterparty_key,
                PaymentPurposeRule.direction == direction(tx),
            )
            .with_for_update()
        )
        if rule and (not rule.active or rule.purpose != purpose):
            raise ValueError("Existing recipient knowledge conflicts; review before replacing")
        if not rule:
            rule = PaymentPurposeRule(
                account_id=tx.account_id,
                counterparty_key=tx.counterparty_key,
                direction=direction(tx),
                purpose=purpose,
            )
            s.add(rule)
            s.flush()
    before = {
        "category": tx.category,
        "source": tx.category_source,
        "status": tx.category_status,
        "purpose_confirmation": tx.purpose_confirmation,
    }
    # Historical corrections remain manual even when they establish a reusable rule.
    assign(tx, purpose, scope="recipient" if future else "payment")
    s.add(
        Audit(
            actor="owner",
            action="payment_purpose_confirmed",
            detail={
                "transaction_id": tx.id,
                "purpose": purpose,
                "before": before,
                "version": tx.version,
                "rule_id": rule.id if rule else None,
                "scope": "recipient" if future else "payment",
            },
        )
    )


def apply_known_purpose(s, tx, data):
    old = tx.purpose_confirmation
    if data.get("counterparty"):
        tx.counterparty_key = counterparty_key(data)
    if old and old.get("snapshot") != snapshot(tx):
        tx.purpose_confirmation = None
        if tx.category_source != "manual" or tx.category == PURPOSES[old["purpose"]][1]:
            tx.category, tx.category_source, tx.category_status = None, None, "pending"
        tx.version += 1
        s.add(
            Audit(
                actor="system",
                action="payment_purpose_invalidated",
                detail={"transaction_id": tx.id, "reason": "bank_evidence_changed"},
            )
        )
    if tx.declined or not tx.amount or tx.category_source == "manual" or not tx.counterparty_key:
        return
    rule = s.scalar(
        select(PaymentPurposeRule).where(
            PaymentPurposeRule.account_id == tx.account_id,
            PaymentPurposeRule.counterparty_key == tx.counterparty_key,
            PaymentPurposeRule.direction == direction(tx),
            PaymentPurposeRule.active.is_(True),
        )
    )
    if rule and (
        not confirmed_purpose(tx) or (tx.purpose_confirmation or {}).get("rule_id") != rule.id
    ):
        assign(tx, rule.purpose, rule_id=rule.id, scope="recipient")
        s.add(
            Audit(
                actor="system",
                action="payment_purpose_rule_applied",
                detail={"transaction_id": tx.id, "rule_id": rule.id, "purpose": rule.purpose},
            )
        )


def export_rules(s):
    """Owner administration output excludes counterparty fingerprints."""
    return [
        {
            "id": r.id,
            "account_id": r.account_id,
            "direction": r.direction,
            "purpose": r.purpose,
            "active": r.active,
        }
        for r in s.scalars(select(PaymentPurposeRule).order_by(PaymentPurposeRule.created_at))
    ]


def revoke_rule(s, rule_id):
    from monzo_backend.models import Transaction

    rule = s.scalar(
        select(PaymentPurposeRule).where(PaymentPurposeRule.id == rule_id).with_for_update()
    )
    if not rule or not rule.active:
        raise ValueError("Active rule not found")
    rule.active = False
    affected = []
    returned_to_review = 0
    for tx in s.scalars(
        select(Transaction)
        .where(Transaction.purpose_confirmation["rule_id"].astext == rule_id)
        .with_for_update()
    ):
        tx.purpose_confirmation = None
        tx.version += 1
        tx.updated_at = now()
        if tx.category_source == "rule":
            tx.category, tx.category_source, tx.category_status = None, "review", "needs_review"
            returned_to_review += 1
        affected.append(tx.id)
    s.add(
        Audit(
            actor="owner",
            action="payment_purpose_rule_revoked",
            detail={"rule_id": rule_id, "transaction_ids": affected},
        )
    )
    return {"revoked": rule_id, "payments_returned_to_review": returned_to_review}


if __name__ == "__main__":
    import argparse

    from monzo_backend.db import session

    parser = argparse.ArgumentParser(
        description="Inspect or revoke owner-confirmed recipient knowledge"
    )
    parser.add_argument("--revoke-rule")
    args = parser.parse_args()
    with session() as s, s.begin():
        print(json.dumps(revoke_rule(s, args.revoke_rule) if args.revoke_rule else export_rules(s)))
