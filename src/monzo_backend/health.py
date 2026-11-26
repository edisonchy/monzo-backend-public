"""Database observations for Settings; no bank, provider or email calls."""

from datetime import timedelta

from sqlalchemy import func, select

from monzo_backend.config import settings
from monzo_backend.coverage import accounts_with_cutoff
from monzo_backend.models import (
    Account,
    Connection,
    Control,
    Job,
    Notification,
    OAuthState,
    WebhookEvent,
    now,
)


def oauth_configured():
    cfg = settings()
    return bool(
        cfg.client_id
        and cfg.client_secret.get_secret_value()
        and cfg.dashboard_origin
        and cfg.redirect_uri == cfg.dashboard_origin + "/oauth/callback"
    )


def queue_health(s, queue, observed):
    counts = dict(
        s.execute(
            select(Job.state, func.count()).where(Job.queue == queue).group_by(Job.state)
        ).all()
    )
    oldest = s.scalar(
        select(func.min(Job.created_at)).where(Job.queue == queue, Job.state == "pending")
    )
    overdue = s.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.queue == queue, Job.state == "running", Job.lease_until < observed)
    )
    return {
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
        "overdue": overdue,
        "oldest_pending_at": oldest,
        "delayed": bool(oldest and oldest < observed - timedelta(minutes=45)),
    }


def snapshot(s):
    observed = now()
    ctl, conn = s.get(Control, 1), s.get(Connection, 1)
    queues = {q: queue_health(s, q, observed) for q in ("sync", "category")}
    pending, oldest, attempts = s.execute(
        select(
            func.count(), func.min(Notification.created_at), func.max(Notification.attempts)
        ).where(Notification.delivered_at.is_(None))
    ).one()
    last_ack = s.scalar(select(func.max(Notification.delivered_at)))
    webhooks = dict(
        s.execute(
            select(WebhookEvent.account_id, func.max(WebhookEvent.received_at)).group_by(
                WebhookEvent.account_id
            )
        ).all()
    )
    accounts = []
    enabled_cutoffs = []
    for a, cutoff in s.execute(
        accounts_with_cutoff()
        .where(Account.connection_id == 1)
        .where(Account.kind.in_(("uk_retail", "uk_monzo_flex")))
        .order_by(Account.kind, Account.id)
    ):
        if a.enabled:
            enabled_cutoffs.append(cutoff)
        state = (
            "disabled"
            if not a.enabled
            else "never_synced"
            if not cutoff and not a.last_sync_at
            else "unknown"
            if not cutoff
            else "stale"
            if cutoff < observed - timedelta(minutes=45)
            else "current"
        )
        accounts.append(
            {
                "id": a.id,
                "kind": a.kind,
                "state": state,
                "last_sync_at": a.last_sync_at,
                "checked_through": cutoff,
                "history_status": a.history_status,
                "last_webhook_at": webhooks.get(a.id),
            }
        )
    oauth = s.scalar(select(OAuthState).order_by(OAuthState.expires_at.desc()).limit(1))
    outcome = oauth.outcome if oauth else "none"
    if oauth and outcome in ("pending", "exchanging") and oauth.expires_at <= observed:
        outcome = "expired"
    return {
        "observed_at": observed,
        "stale_after_minutes": 45,
        "accounts": accounts,
        "queues": queues,
        "connection": {
            "state": conn.state if conn else "not_connected",
            "last_sync_at": conn.last_sync_at if conn else None,
            "checked_through": min(enabled_cutoffs)
            if enabled_cutoffs and all(enabled_cutoffs)
            else None,
            "oauth_configured": oauth_configured(),
            "reconnect_state": outcome,
        },
        "categorisation": {
            "state": ctl.state,
            "incident_id": ctl.incident_id,
            "last_success_at": ctl.last_success_at,
            "pending": queues["category"]["pending"],
            "oldest_pending_at": queues["category"]["oldest_pending_at"],
        },
        "notifications": {
            "pending": pending,
            "oldest_pending_at": oldest,
            "max_attempts": attempts or 0,
            "last_acknowledged_at": last_ack,
            "state": "delayed"
            if oldest and oldest < observed - timedelta(minutes=5)
            else "pending"
            if pending
            else "idle",
        },
        "webhooks": [
            {"kind": a["kind"], "last_received_at": a["last_webhook_at"]} for a in accounts
        ],
        "ai_enabled": settings().ai_enabled,
        "ai_auto_accept": settings().ai_auto_accept,
    }
