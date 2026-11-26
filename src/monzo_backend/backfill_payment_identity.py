"""Bounded, atomic display-only enrichment of unnamed supported payments."""

import argparse
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select

from monzo_backend.card_checks import CARD_CHECK
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.http import request
from monzo_backend.models import Audit, Connection, Transaction, now
from monzo_backend.monzo import decrypt
from monzo_backend.payment_identity import apply_identity
from monzo_backend.spending import eligible_accounts, exclusions


def fetch_identity(transaction_id):
    # Only the sync worker rotates tokens. An owner backfill borrows a valid
    # token, and stops on expiry/error rather than risking concurrent rotation.
    with session() as s:
        conn = s.get(Connection, 1)
        if not conn or conn.expires_at <= now() + timedelta(seconds=60):
            raise ValueError("Wait for the sync worker to refresh bank access")
        token = decrypt(conn.access_token)
    return request(
        "GET",
        settings().api_url + "/transactions/" + transaction_id,
        service="monzo",
        params={"expand[]": "merchant"},
        headers={"Authorization": f"Bearer {token}"},
    )["transaction"]


def backfill(*, expected_count, apply=False, fetch=fetch_identity):
    with session() as s:
        excluded = exclusions(s)
        ids = s.scalars(
            select(Transaction.id)
            .where(
                Transaction.account_id.in_(eligible_accounts()),
                Transaction.declined.is_(False),
                ~CARD_CHECK,
                func.length(func.trim(Transaction.merchant_name)) == 0,
            )
            .order_by(Transaction.id)
        ).all()
        ids = [tid for tid in ids if tid not in excluded]
    if len(ids) != expected_count or not 0 < len(ids) <= 200:
        raise ValueError("Unnamed payment selection changed or exceeds 200; inspect before retry")
    if not apply:
        return {"selected": len(ids), "applied": False}
    # All reads succeed before any mutation. Never persist the full bank payload.
    payloads = [(tid, fetch(tid)) for tid in ids]
    counts = Counter()
    with session() as s, s.begin():
        for tid, data in payloads:
            tx = s.scalar(select(Transaction).where(Transaction.id == tid).with_for_update())
            if not tx or tx.merchant_name.strip():
                raise ValueError("Selected payment changed during backfill")
            before = (tx.payment_name, tx.payment_name_source, tx.counterparty_name)
            apply_identity(tx, data)
            counts[tx.payment_name_source or "unresolved"] += 1
            counts["changed"] += before != (
                tx.payment_name,
                tx.payment_name_source,
                tx.counterparty_name,
            )
        result = {"selected": len(ids), "applied": True, **counts}
        s.add(
            Audit(
                actor="owner",
                action="payment_identity_backfilled",
                detail={**result, "transaction_ids": ids},
            )
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(backfill(expected_count=args.expected_count, apply=args.apply))
