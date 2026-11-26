"""Resolve existing bank-verified pot transfers, with no provider or bank requests."""

from sqlalchemy import select

from monzo_backend.db import session
from monzo_backend.domain import assign_pot_transfer
from monzo_backend.models import Audit, Transaction
from monzo_backend.spending import POT_EVIDENCE


def backfill():
    updated = 0
    with session() as s, s.begin():
        rows = s.scalars(
            select(Transaction).where(POT_EVIDENCE).order_by(Transaction.id).with_for_update()
        ).all()
        for tx in rows:
            updated += assign_pot_transfer(s, tx)
        result = {"verified": len(rows), "updated": updated, "provider_requests": 0}
        s.add(Audit(actor="owner", action="pot_transfers_backfilled", detail=result))
    return result


if __name__ == "__main__":
    print(backfill())
