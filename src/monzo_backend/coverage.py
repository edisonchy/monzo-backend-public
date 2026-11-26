"""Only completed paginated windows within known recent API access establish coverage."""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from monzo_backend.models import Account, ReportingCoverage, now, uid


def accounts_with_cutoff():
    windows = (
        select(ReportingCoverage.account_id, func.max(ReportingCoverage.end_at).label("cutoff"))
        .group_by(ReportingCoverage.account_id)
        .subquery()
    )
    return select(Account, windows.c.cutoff).outerjoin(windows, Account.id == windows.c.account_id)


def window_start(payload):
    value = payload.get("coverage_since", payload.get("since", ""))
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except ValueError:
        # A cursor without a preserved starting timestamp cannot establish a window.
        return None


def record_completed_window(s, account_id, payload):
    start = window_start(payload)
    if start is None:
        return
    end = datetime.fromisoformat(payload["before"].replace("Z", "+00:00"))
    observed = now()
    # Developer API recent access is bounded. Historical imports may exceed it;
    # preserve unknown coverage outside the recent window until independently evidenced.
    # The bank since boundary is exclusive; never claim coverage of that instant.
    start = max(start + timedelta(microseconds=1), observed - timedelta(days=89))
    end = min(end, observed)
    if start >= end:
        return
    stmt = insert(ReportingCoverage).values(
        id=uid(),
        account_id=account_id,
        start_at=start,
        end_at=end,
        source="monzo_recent_paginated",
        observed_at=observed,
    )
    s.execute(
        stmt.on_conflict_do_update(
            index_elements=["account_id", "start_at", "end_at", "source"],
            set_={"observed_at": observed},
        )
    )
