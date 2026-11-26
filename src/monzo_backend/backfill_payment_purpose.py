"""Apply an explicit owner-confirmation manifest atomically with fresh bank evidence."""

import argparse
import json
from collections import Counter

from sqlalchemy import select

from monzo_backend.backfill_payment_identity import fetch_identity
from monzo_backend.db import session
from monzo_backend.models import Audit, Transaction
from monzo_backend.payment_purpose import confirm, counterparty_key


def backfill(manifest, *, apply=False, fetch=fetch_identity):
    rows = manifest["payments"]
    batch = manifest["batch_id"]
    if not isinstance(batch, str) or not 0 < len(batch) <= 100:
        raise ValueError("Invalid batch identity")
    ids = [r["id"] for r in rows]
    if not 0 < len(ids) <= 200 or len(set(ids)) != len(ids):
        raise ValueError("Expected 1–200 distinct explicit payments")
    with session() as s:
        previous = s.scalar(
            select(Audit).where(
                Audit.action == "payment_purposes_backfilled",
                Audit.detail["batch_id"].astext == batch,
            )
        )
        if previous:
            if previous.detail["payments"] != rows:
                raise ValueError("Batch identity reused with different confirmations")
            return {"already_applied": True, "selected": len(ids)}
    # A read or permissions failure aborts before any writes.
    payloads = {tid: fetch(tid) for tid in ids}
    counts = Counter()
    with session() as s, s.begin():
        for row in sorted(rows, key=lambda r: r["id"]):
            tx = s.scalar(select(Transaction).where(Transaction.id == row["id"]).with_for_update())
            data = payloads[row["id"]]
            if (
                not tx
                or tx.version != row["expected_version"]
                or tx.counterparty_name != row["counterparty_name"]
                or data.get("id") != tx.id
                or data.get("account_id") != tx.account_id
                or data.get("amount") != tx.amount
                or data.get("currency") != tx.currency
                or tx.amount != row["amount"]
                or tx.currency != row["currency"]
                or (data.get("counterparty") or {}).get("name") != row["counterparty_name"]
            ):
                raise ValueError("Payment or bank evidence changed; regenerate and review manifest")
            key = counterparty_key(data)
            # Requested recipient memory requires bank evidence. Otherwise confirm just this payment.
            future = bool(row["future"] and key)
            counts[row["purpose"]] += 1
            counts["recipient_scoped" if future else "payment_scoped"] += 1
            if apply:
                tx.counterparty_key = key
                confirm(
                    s, tx, row["purpose"], expected_version=row["expected_version"], future=future
                )
        result = {"selected": len(rows), "applied": apply, **counts}
        if apply:
            s.add(
                Audit(
                    actor="owner",
                    action="payment_purposes_backfilled",
                    detail={**result, "batch_id": batch, "payments": rows},
                )
            )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Owner-reviewed JSON manifest; '-' for stdin")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import sys

    manifest = json.load(sys.stdin if args.manifest == "-" else open(args.manifest))
    print(json.dumps(backfill(manifest, apply=args.apply)))
