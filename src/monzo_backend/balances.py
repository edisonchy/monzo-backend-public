"""Verified Current/pot observations. No balance arithmetic or direct read-time bank access."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from monzo_backend.db import session
from monzo_backend.domain import enqueue, owns_sync_lease, sync_result
from monzo_backend.http import ServiceError
from monzo_backend.models import (
    Account,
    BalanceObservation,
    BalanceSnapshot,
    DashboardRevision,
    Job,
    now,
)

SOURCES = ("current", "pots")
TZ = ZoneInfo("Europe/London")
STALE_MINUTES = 45


def schedule(s, account_id):
    account = s.get(Account, account_id)
    if (
        not account
        or account.connection_id != 1
        or not account.enabled
        or account.kind != "uk_retail"
    ):
        return []
    # Serialize enqueue checks across webhook/reconciliation callers and slot boundaries.
    s.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": "balance-schedule:" + account_id},
    )
    slot = int(now().timestamp()) // 900
    ids = []
    for source in SOURCES:
        observation = s.get(BalanceObservation, (account_id, source))
        if (
            observation
            and observation.outcome == "unsupported"
            and observation.attempted_at > now() - timedelta(days=1)
        ):
            continue
        active = s.scalar(
            select(Job.id)
            .where(
                Job.kind == "collect_balance",
                Job.state.in_(("pending", "running")),
                Job.payload["account_id"].as_string() == account_id,
                Job.payload["source"].as_string() == source,
            )
            .limit(1)
        )
        if active:
            ids.append(active)
            continue
        ids.append(
            enqueue(
                s,
                "sync",
                "collect_balance",
                f"balance:{account_id}:{source}:{slot}",
                {"account_id": account_id, "source": source},
            )
        )
    return ids


def normalise(source, account_id, value):
    def amount(item):
        v = item.get("balance")
        # JSON clients must be able to represent the integer exactly.
        if type(v) is not int or abs(v) > 2**53 - 1 or item.get("currency") != "GBP":
            raise ValueError()
        return v

    try:
        if source == "current":
            return {"amount_minor": amount(value), "currency": "GBP", "basis": "available"}
        pots = value["pots"]
        if not isinstance(pots, list) or len(pots) > 1000:
            raise ValueError()
        active, seen = [], set()
        for pot in pots:
            if pot.get("current_account_id") != account_id or type(pot.get("deleted")) is not bool:
                raise ValueError()
            pid = pot.get("id")
            if (
                not isinstance(pid, str)
                or not pid.startswith("pot_")
                or len(pid) > 128
                or pid in seen
            ):
                raise ValueError()
            seen.add(pid)
            if pot["deleted"]:
                continue
            name = pot.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 500:
                raise ValueError()
            active.append(
                {
                    "id": pid,
                    "name": name,
                    "amount_minor": amount(pot),
                    "currency": "GBP",
                    "basis": "pot_balance",
                }
            )
        return {"pots": sorted(active, key=lambda p: p["id"])}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ServiceError("monzo_balance_response_invalid", False) from None


def eligible(s, work, *, lock=True):
    job = s.get(Job, work["id"], with_for_update=lock)
    account = s.get(Account, work["payload"]["account_id"])
    return bool(
        owns_sync_lease(job, work, now())
        and account
        and account.connection_id == 1
        and account.enabled
        and account.kind == "uk_retail"
    )


def save(work, started, *, data=None, outcome="ok", complete=False, error=None):
    account_id, source = work["payload"]["account_id"], work["payload"]["source"]
    with sync_result(work if complete else None, error=error) as s:
        if not eligible(s, work):
            return False
        values = {
            "account_id": account_id,
            "source": source,
            "attempted_at": started,
            "outcome": outcome,
        }
        if data is not None:
            values.update(data=data, retrieved_at=now(), provider_observed_at=None)
        stmt = insert(BalanceObservation).values(**values)
        updated = s.execute(
            stmt.on_conflict_do_update(
                index_elements=[BalanceObservation.account_id, BalanceObservation.source],
                set_={k: v for k, v in values.items() if k not in ("account_id", "source")},
                where=BalanceObservation.attempted_at < started,
            ).returning(BalanceObservation.account_id)
        ).scalar_one_or_none()
        if updated and data is not None:
            snapshot = {k: v for k, v in values.items() if k != "outcome"}
            snapshot["day"] = values["retrieved_at"].astimezone(TZ).date()
            s.execute(
                insert(BalanceSnapshot)
                .values(**snapshot)
                .on_conflict_do_update(
                    index_elements=[
                        BalanceSnapshot.account_id,
                        BalanceSnapshot.source,
                        BalanceSnapshot.day,
                    ],
                    set_={
                        k: v
                        for k, v in snapshot.items()
                        if k not in ("account_id", "source", "day")
                    },
                    where=BalanceSnapshot.attempted_at < started,
                )
            )
        return bool(updated)


def collect(work, *, complete=False):
    from monzo_backend.monzo import bank_get

    source = work["payload"].get("source")
    if source not in SOURCES:
        raise ServiceError("balance_source_invalid", False)
    with session() as s, s.begin():
        active = eligible(s, work, lock=False)
    if not active:
        if complete:
            with sync_result(work):
                return
        return
    started = now()
    account_id = work["payload"]["account_id"]
    try:
        data = bank_get(
            "/balance" if source == "current" else "/pots",
            {"account_id" if source == "current" else "current_account_id": account_id},
        )
        data = normalise(source, account_id, data)
    except ServiceError as exc:
        unsupported = exc.code in ("monzo_http_403", "monzo_http_404")
        error = ServiceError("monzo_balance_unsupported", False) if unsupported else exc
        save(
            work,
            started,
            outcome="unsupported" if unsupported else "error",
            complete=complete,
            error=error,
        )
        if complete:
            return
        raise error from None
    save(work, started, data=data, complete=complete)


def report(account_id=None):
    with session() as s:
        s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return read_report(s, account_id=account_id)


def read_report(s, *, account_id=None, observed=None):
    """Use the caller's snapshot and privileges; never open another connection."""
    observed = observed or now()
    accounts = s.scalars(
        select(Account)
        .where(Account.connection_id == 1, Account.enabled.is_(True), Account.kind == "uk_retail")
        .order_by(Account.id)
    ).all()
    if account_id is not None:
        accounts = [a for a in accounts if a.id == account_id]
        if not accounts:
            raise HTTPException(
                422, "Choose an enabled Current account; Flex balances are unverified"
            )
    items = []
    for account in accounts:
        for source in SOURCES:
            row = s.get(BalanceObservation, (account.id, source))
            state = "unavailable"
            if row:
                if row.outcome == "unsupported":
                    state = "unsupported"
                elif row.data is not None:
                    state = (
                        "fresh"
                        if row.outcome == "ok"
                        and row.retrieved_at > observed - timedelta(minutes=STALE_MINUTES)
                        else "stale"
                    )
            snapshots = s.scalars(
                select(BalanceSnapshot)
                .where(
                    BalanceSnapshot.account_id == account.id,
                    BalanceSnapshot.source == source,
                    BalanceSnapshot.day >= observed.astimezone(TZ).date() - timedelta(days=30),
                )
                .order_by(BalanceSnapshot.day)
            ).all()
            items.append(
                {
                    "account_id": account.id,
                    "source": source,
                    "state": state,
                    "last_attempt_at": row.attempted_at if row else None,
                    "fetch_outcome": row.outcome if row else None,
                    "retrieved_at": row.retrieved_at if row else None,
                    "provider_observed_at": None,
                    "data": row.data if row and state != "unsupported" else None,
                    "history": [
                        {"day": x.day, "retrieved_at": x.retrieved_at, "data": x.data}
                        for x in snapshots
                    ],
                }
            )
    return {
        "observed_at": observed,
        "revision": s.scalar(select(DashboardRevision.revision).where(DashboardRevision.id == 1))
        or 0,
        "currency": "GBP",
        "account_id": account_id,
        "stale_after_minutes": STALE_MINUTES,
        "snapshot_timezone": "Europe/London",
        "items": items,
        "excluded": ["flex", "aggregate_savings"],
    }
