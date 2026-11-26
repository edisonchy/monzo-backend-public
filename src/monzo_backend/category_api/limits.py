"""Database-backed rolling-minute admission, independent of transaction success."""

import hashlib
import math

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from monzo_backend.models import CategoryRateLimit


class RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def admit(session_factory, actor, bucket):
    if bucket not in {"preview", "commit"}:
        raise ValueError("Unknown admission bucket")
    key = hashlib.sha256((actor.issuer + "\0" + actor.subject).encode()).hexdigest()
    limit = 10 if bucket == "preview" else 5
    with session_factory() as s, s.begin():
        s.execute(
            insert(CategoryRateLimit)
            .values(
                owner_key=key,
                bucket=bucket,
                attempts=[],
                credits=2,
                updated_at=s.scalar(text("SELECT clock_timestamp()")),
            )
            .on_conflict_do_nothing()
        )
        row = s.scalar(
            select(CategoryRateLimit)
            .where(
                CategoryRateLimit.owner_key == key,
                CategoryRateLimit.bucket == bucket,
            )
            .with_for_update()
        )
        timestamp = s.scalar(text("SELECT clock_timestamp()"))
        seconds = timestamp.timestamp()
        recent = [v for v in row.attempts if v > seconds - 60]
        credits = min(2, row.credits + max(0, (timestamp - row.updated_at).total_seconds()) / 12)
        wait = max(0, recent[0] + 60 - seconds) if len(recent) >= limit else 0
        if bucket == "commit" and credits < 1:
            wait = max(wait, (1 - credits) * 12)
        if wait:
            raise RateLimited(max(1, math.ceil(wait)))
        row.attempts = [*recent, seconds]
        row.credits = credits - 1 if bucket == "commit" else credits
        row.updated_at = timestamp
