"""Bounded, resumable bank-evidence backfill. No category or queue changes.

Run inside the private API container: python -m monzo_backend.backfill_reporting --limit 2000
Existing bank credentials stay in the application's normal encrypted token store.
"""

import argparse
import time

from sqlalchemy import select

from monzo_backend.db import session
from monzo_backend.http import ServiceError
from monzo_backend.models import Audit, Transaction
from monzo_backend.monzo import bank_get
from monzo_backend.spending import apply_bank_facts, eligible_accounts


def backfill(limit=100, delay=0.2):
    with session() as s:
        ids = s.scalars(
            select(Transaction.id)
            .where(
                Transaction.account_id.in_(eligible_accounts()),
                Transaction.reporting_checked_at.is_(None),
            )
            .order_by(Transaction.created_at.desc(), Transaction.id)
            .limit(limit)
        ).all()
    updated, skipped = 0, 0
    try:
        for tx_id in ids:
            for attempt in range(3):
                try:
                    data = bank_get("/transactions/" + tx_id)["transaction"]
                    break
                except ServiceError as exc:
                    if not exc.retryable or attempt == 2 or exc.retry_after > 30:
                        raise
                    time.sleep(max(exc.retry_after, 2**attempt))
            with session() as s, s.begin():
                tx = s.scalar(select(Transaction).where(Transaction.id == tx_id).with_for_update())
                if tx and tx.reporting_checked_at is None:
                    try:
                        apply_bank_facts(tx, data)
                        updated += 1
                    except ValueError:
                        skipped += 1
                else:
                    skipped += 1
            if (updated + skipped) % 100 == 0:
                print(f"Reporting evidence: {updated} updated, {skipped} skipped", flush=True)
            time.sleep(delay)
    finally:
        with session() as s, s.begin():
            s.add(
                Audit(
                    actor="owner",
                    action="reporting_evidence_backfill",
                    detail={
                        "selected": len(ids),
                        "updated": updated,
                        "skipped": skipped,
                    },
                )
            )
    return {"selected": len(ids), "updated": updated, "skipped": skipped}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=100, choices=range(1, 10001), metavar="1..10000"
    )
    args = parser.parse_args()
    try:
        print(backfill(args.limit), flush=True)
    except ServiceError as exc:
        print("Backfill stopped safely: " + exc.code, flush=True)
        raise SystemExit(1) from None
