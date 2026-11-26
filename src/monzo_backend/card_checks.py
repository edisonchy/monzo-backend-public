"""Explicit owner-confirmed zero-value card checks; never infer from amount alone."""

from fastapi import HTTPException
from sqlalchemy import and_, select

from monzo_backend.models import Account, Audit, Transaction, now
from monzo_backend.spending import ACCOUNT_KINDS

CARD_CHECK = and_(
    Transaction.transaction_type == "card_verification",
    Transaction.amount == 0,
    Transaction.declined.is_(False),
)


def is_card_check(tx):
    return tx.transaction_type == "card_verification" and tx.amount == 0 and not tx.declined


def exclude_card_check(tx):
    tx.category, tx.category_source, tx.category_status = None, "owner_verification", "excluded"


def confirm(s, tx_id, expected_version):
    tx = s.scalar(select(Transaction).where(Transaction.id == tx_id).with_for_update())
    if not tx or s.get(Account, tx.account_id).kind not in ACCOUNT_KINDS:
        raise HTTPException(404, "Payment not found")
    if tx.version != expected_version:
        raise HTTPException(409, "Payment changed; reload before confirming")
    if tx.amount != 0 or tx.declined:
        raise HTTPException(422, "Only a non-declined zero-value payment can be confirmed")
    if is_card_check(tx):
        return {"id": tx.id, "version": tx.version, "card_verification": True}
    before = {
        "type": tx.transaction_type,
        "category": tx.category,
        "source": tx.category_source,
        "status": tx.category_status,
    }
    tx.transaction_type = "card_verification"
    exclude_card_check(tx)
    tx.version += 1
    tx.updated_at = now()
    s.add(
        Audit(
            actor="owner",
            action="card_verification_confirmed",
            detail={
                "transaction_id": tx.id,
                "evidence": "explicit_owner_confirmation",
                "previous": before,
                "version": tx.version,
            },
        )
    )
    return {"id": tx.id, "version": tx.version, "card_verification": True}
