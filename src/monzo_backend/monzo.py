from datetime import datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import or_, select

from monzo_backend import balances
from monzo_backend.card_checks import CARD_CHECK
from monzo_backend.config import settings
from monzo_backend.coverage import record_completed_window, window_start
from monzo_backend.db import session
from monzo_backend.domain import enqueue, ingest, local_sync_transaction, notify, sync_result
from monzo_backend.http import ServiceError, request
from monzo_backend.models import Account, Audit, Connection, OAuthState, Transaction, now
from monzo_backend.payment_identity import UNKNOWN, apply_identity
from monzo_backend.spending import ACCOUNT_KINDS, DISPLAY_MERCHANT


def cipher():
    return Fernet(settings().encryption_key.get_secret_value().encode())


def encrypt(value):
    return cipher().encrypt(value.encode()).decode() if value else None


def decrypt(value):
    return cipher().decrypt(value.encode()).decode() if value else None


def save_tokens(tokens, expected_user=None, oauth_state_hash=None):
    cfg = settings()
    if (
        not isinstance(tokens, dict)
        or not isinstance(tokens.get("access_token"), str)
        or not tokens["access_token"]
    ):
        raise ServiceError("monzo_token_response_invalid", False)
    try:
        expires_in = int(tokens.get("expires_in", 0))
    except (ValueError, TypeError):
        raise ServiceError("monzo_token_response_invalid", False) from None
    if (
        expires_in < 0
        or expires_in > 86400
        or (
            tokens.get("refresh_token") is not None and not isinstance(tokens["refresh_token"], str)
        )
    ):
        raise ServiceError("monzo_token_response_invalid", False)
    access = tokens["access_token"]
    identity = request(
        "GET",
        cfg.api_url + "/ping/whoami",
        service="monzo",
        headers={"Authorization": f"Bearer {access}"},
    )
    if (
        not isinstance(identity, dict)
        or not identity.get("authenticated")
        or not identity.get("user_id")
    ):
        raise ServiceError("monzo_identity_invalid", False)
    if expected_user and identity["user_id"] != expected_user:
        raise ServiceError("monzo_user_mismatch", False)
    with session() as s, s.begin():
        if oauth_state_hash:
            state = s.scalar(
                select(OAuthState)
                .where(OAuthState.state_hash == oauth_state_hash)
                .with_for_update()
            )
            if not state or state.outcome != "exchanging" or state.expires_at <= now():
                raise ServiceError("monzo_oauth_superseded", False)
            state.outcome = "connected"
        conn = s.scalar(select(Connection).where(Connection.id == 1).with_for_update())
        if conn and conn.user_id != identity["user_id"]:
            raise ServiceError("monzo_account_owner_change", False)
        if not conn:
            conn = Connection(id=1, user_id=identity["user_id"])
            s.add(conn)
        conn.access_token = encrypt(access)
        conn.refresh_token = encrypt(tokens.get("refresh_token"))
        conn.expires_at = now() + timedelta(seconds=expires_in)
        conn.state = "awaiting_approval"
        s.add(Audit(actor="owner", action="monzo_connected"))
        enqueue(
            s,
            "sync",
            "discover",
            "discover:" + conn.access_token[-32:],
            {"reconnect": True} if oauth_state_hash else {},
        )


def require_reconnection(s, conn, reason):
    conn.state = "reauthorisation_required"
    notify(
        s,
        "reauth:" + conn.access_token[-32:],
        "Monzo access needs attention",
        f"Bank access requires reconnection: {reason}. Transaction synchronisation is interrupted.",
    )


def connection_error(code, expected_access=None, expected_token=None):
    with session() as s, s.begin():
        conn = s.scalar(select(Connection).where(Connection.id == 1).with_for_update())
        if (
            conn
            and (expected_access is None or conn.access_token == expected_access)
            and (expected_token is None or decrypt(conn.access_token) == expected_token)
        ):
            require_reconnection(s, conn, code)
            return ServiceError("monzo_reauthorisation_required", False)
        # A reconnect replaced the rejected credentials while the request was in flight.
        return ServiceError("monzo_credentials_changed", True)


def access_token():
    cfg = settings()
    with local_sync_transaction() as s:
        conn = s.scalar(select(Connection).where(Connection.id == 1).with_for_update())
        if not conn:
            raise ServiceError("monzo_not_connected", False)
        if conn.state == "refreshing":
            # Only one sync worker is permitted. A persisted refresh intent here
            # means the previous process died with an uncertain rotation outcome.
            require_reconnection(s, conn, "token_refresh_interrupted")
            s.commit()
            raise ServiceError("monzo_reauthorisation_required", False)
        if conn.state == "reauthorisation_required":
            raise ServiceError("monzo_reauthorisation_required", False)
        if conn.expires_at > now() + timedelta(seconds=60):
            return decrypt(conn.access_token)
        refresh, old_access = decrypt(conn.refresh_token), conn.access_token
        if not refresh or not cfg.client_id or not cfg.client_secret.get_secret_value():
            require_reconnection(s, conn, "refresh_credentials_missing")
            s.commit()
            raise ServiceError("monzo_reauthorisation_required", False)
        conn.state = "refreshing"  # Commit intent before a one-time token rotation.
    try:
        tokens = request(
            "POST",
            cfg.api_url + "/oauth2/token",
            service="monzo",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret.get_secret_value(),
            },
        )
        if (
            not tokens.get("access_token")
            or not tokens.get("refresh_token")
            or not tokens.get("expires_in")
        ):
            raise ServiceError("monzo_token_response_invalid", False)
        with session() as s, s.begin():
            conn = s.scalar(select(Connection).where(Connection.id == 1).with_for_update())
            if conn.access_token != old_access:
                raise ServiceError("monzo_credentials_changed", False)
            conn.access_token, conn.refresh_token = (
                encrypt(tokens["access_token"]),
                encrypt(tokens["refresh_token"]),
            )
            conn.expires_at = now() + timedelta(seconds=int(tokens["expires_in"]))
            conn.state = "healthy"
        return tokens["access_token"]
    except ServiceError:
        # The remote service may have consumed the token even on network failure.
        raise connection_error("refresh_outcome_uncertain", expected_access=old_access) from None


def bank_get(path, params=None):
    token = access_token()
    try:
        return request(
            "GET",
            settings().api_url + path,
            service="monzo",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    except ServiceError as exc:
        if exc.code == "monzo_http_401":
            raise connection_error("access_rejected", expected_token=token) from None
        raise


def discovery_accounts(response, *, reconnect):
    accounts = response.get("accounts") if isinstance(response, dict) else None
    if not isinstance(accounts, list):
        raise ServiceError("monzo_accounts_response_invalid", False)
    for item in accounts:
        if not isinstance(item, dict):
            raise ServiceError("monzo_accounts_response_invalid", False)
        account_id = item.get("id")
        kind = item.get("type", None if reconnect else "unknown")
        if (
            not isinstance(account_id, str)
            or not account_id.strip()
            or not isinstance(kind, str)
            or not kind.strip()
        ):
            raise ServiceError("monzo_accounts_response_invalid", False)
    return accounts


def execute(work):
    payload = work["payload"]
    if work["kind"] == "collect_balance":
        return balances.collect(work, complete=True)
    if work["kind"] == "discover":
        accounts = discovery_accounts(
            bank_get("/accounts"), reconnect=payload.get("reconnect", False)
        )
        with sync_result(work) as s:
            conn = s.get(Connection, 1)
            conn.state = "healthy"
            for item in accounts:
                acct = s.get(Account, item["id"])
                if payload.get("reconnect"):
                    if item.get("type", "unknown") not in ACCOUNT_KINDS or (
                        acct and not acct.enabled
                    ):
                        continue
                    if not acct:
                        acct = Account(
                            id=item["id"],
                            connection_id=1,
                            kind=item["type"],
                            history_status="incomplete",
                        )
                        s.add(acct)
                    end = now()
                    # Reconnection restores recent access, never starts a history import.
                    enqueue(
                        s,
                        "sync",
                        "sync_page",
                        f"reconnect:{work['id']}:{acct.id}",
                        {
                            "account_id": acct.id,
                            "since": (end - timedelta(days=7)).isoformat(),
                            "before": end.isoformat(),
                            "historical": False,
                            "reconnect_cutoff": end.isoformat(),
                        },
                    )
                    continue
                if not acct:
                    acct = Account(id=item["id"], connection_id=1, kind=item.get("type", "unknown"))
                    s.add(acct)
                end = now()
                if not item.get("created"):
                    acct.history_status = "incomplete"
                    start = end - timedelta(days=89)
                    historical = False
                else:
                    start = datetime.fromisoformat(item["created"].replace("Z", "+00:00"))
                    if start.tzinfo is None or start > end:
                        raise ServiceError("monzo_account_created_invalid", False)
                    start -= timedelta(seconds=1)
                    historical = True
                    acct.history_status = "importing"
                enqueue(
                    s,
                    "sync",
                    "sync_page",
                    f"initial:{work['id']}:{acct.id}",
                    {
                        "account_id": acct.id,
                        "since": start.isoformat(),
                        "before": min(start + timedelta(days=30), end).isoformat(),
                        "history_end": end.isoformat(),
                        "historical": historical,
                    },
                )
        return
    if work["kind"] == "fetch_payment_identity":
        try:
            item = bank_get("/transactions/" + payload["transaction_id"], {"expand[]": "merchant"})[
                "transaction"
            ]
        except ServiceError as exc:
            if exc.code in ("monzo_http_403", "monzo_http_404"):
                with sync_result(work):
                    return  # Persisted scheduling budget prevents endless historical retries.
            raise
        with sync_result(work) as s:
            tx = s.scalar(
                select(Transaction)
                .where(Transaction.id == payload["transaction_id"])
                .with_for_update()
            )
            if tx and tx.account_id == payload["account_id"]:
                # Financial changes are handled by ordinary sync, never this job.
                if tx.amount == item.get("amount") and tx.currency == item.get("currency"):
                    apply_identity(tx, item)
        return
    if work["kind"] == "fetch_transaction":
        try:
            response = bank_get(
                "/transactions/" + payload["transaction_id"], {"expand[]": "merchant"}
            )
        except ServiceError as exc:
            if payload.get("older_pending") and exc.code in ("monzo_http_403", "monzo_http_404"):
                with sync_result(work) as s:
                    tx = s.get(Transaction, payload["transaction_id"])
                    if tx:
                        tx.pending_check_after = now() + timedelta(days=7)
                    notify(
                        s,
                        f"pending-access:{payload['account_id']}:{int(now().timestamp()) // 604800}",
                        "Older pending payments need attention",
                        "Monzo refused access to one or more unresolved payments. Saved states are preserved; checks retry weekly. This alert is coalesced per account and week.",
                    )
                return
            raise
        item = response.get("transaction") if isinstance(response, dict) else None
        if not isinstance(item, dict):
            raise ServiceError("monzo_transaction_response_invalid", False)
        if item.get("id") != payload["transaction_id"]:
            raise ServiceError("monzo_transaction_id_mismatch", False)
        with sync_result(work) as s:
            ingest(s, payload["account_id"], item)
            balances.schedule(s, payload["account_id"])
        return
    if work["kind"] != "sync_page":
        raise ServiceError("unknown_sync_job", False)
    params = {
        "account_id": payload["account_id"],
        "since": payload["since"],
        "before": payload["before"],
        "limit": 100,
        "expand[]": "merchant",
    }
    try:
        response = bank_get("/transactions", params)
    except ServiceError as exc:
        if exc.code == "monzo_http_403" and payload.get("historical"):
            with sync_result(work) as s:
                cutoff = now()
                acct = s.get(Account, payload["account_id"])
                acct.history_status = "incomplete"
                notify(
                    s,
                    f"history:{work['id']}",
                    "Monzo history import incomplete",
                    "Historical access was refused. Recent transactions will still be synced. Reauthorise to retry older history.",
                )
                enqueue(
                    s,
                    "sync",
                    "sync_page",
                    f"recent-fallback:{work['id']}",
                    {
                        **payload,
                        "historical": False,
                        "since": (cutoff - timedelta(days=89)).isoformat(),
                        "coverage_since": (cutoff - timedelta(days=89)).isoformat(),
                        "before": cutoff.isoformat(),
                        "history_end": cutoff.isoformat(),
                    },
                )
            return
        raise
    items = response.get("transactions") if isinstance(response, dict) else None
    if (
        not isinstance(items, list)
        or len(items) > params["limit"]
        or any(not isinstance(item, dict) for item in items)
    ):
        raise ServiceError("monzo_transactions_response_invalid", False)
    with sync_result(work) as s:
        acct = s.get(Account, payload["account_id"])
        for item in items:
            ingest(s, acct.id, item, allow_ai=not payload.get("reconnect_cutoff"))
        if len(items) == 100:
            # Monzo's cursor is the final item in API order, not a locally sorted timestamp.
            cursor = items[-1]["id"]
            if cursor == payload["since"]:
                raise ServiceError("pagination_not_advancing", False)
            enqueue(
                s,
                "sync",
                "sync_page",
                f"page:{acct.id}:{cursor}:{payload['before']}",
                {
                    **payload,
                    "since": cursor,
                    "coverage_since": window_start(payload).isoformat()
                    if window_start(payload)
                    else "",
                },
            )
        else:
            record_completed_window(s, acct.id, payload)
            window_end = datetime.fromisoformat(payload["before"].replace("Z", "+00:00"))
            history_end = datetime.fromisoformat(
                payload.get("history_end", payload["before"]).replace("Z", "+00:00")
            )
            if window_end < history_end:
                # Bound every request's time range; overlap windows to avoid boundary gaps.
                # Complete all cursor pages in this window before advancing to the next.
                next_end = min(window_end + timedelta(days=30), history_end)
                enqueue(
                    s,
                    "sync",
                    "sync_page",
                    f"window:{acct.id}:{next_end.isoformat()}:{history_end.isoformat()}",
                    {
                        **payload,
                        "since": (window_end - timedelta(seconds=1)).isoformat(),
                        "coverage_since": (window_end - timedelta(seconds=1)).isoformat(),
                        "before": next_end.isoformat(),
                    },
                )
            else:
                acct.last_sync_at = now()
                if payload.get("historical"):
                    acct.history_status = "imported"
                conn = s.get(Connection, 1)
                conn.last_sync_at = now()
                balances.schedule(s, acct.id)


def schedule_sync(s, broad=False):
    if s.scalar(select(Connection.state).where(Connection.id == 1)) == "reauthorisation_required":
        return []
    # Coalesce scheduled requests into one account job per 15-minute slot.
    slot = int(now().timestamp()) // 900
    ids = []
    for acct in s.scalars(select(Account).where(Account.enabled.is_(True))):
        balances.schedule(s, acct.id)
        since = now() - timedelta(days=89 if broad else 7)
        ids.append(
            enqueue(
                s,
                "sync",
                "sync_page",
                f"scheduled:{acct.id}:{slot}:{broad}",
                {
                    "account_id": acct.id,
                    "since": since.isoformat(),
                    "before": now().isoformat(),
                    "historical": False,
                },
            )
        )
    # Least recently scheduled first, persisted across restarts and coalesced by
    # row locking. Recent pending records remain covered by the ordinary scan.
    cutoff = now()
    due = s.scalars(
        select(Transaction)
        .join(Account)
        .where(
            Account.enabled.is_(True),
            Transaction.settled_at.is_(None),
            Transaction.declined.is_(False),
            Transaction.created_at < cutoff - timedelta(days=7),
            or_(
                Transaction.pending_check_after.is_(None), Transaction.pending_check_after <= cutoff
            ),
        )
        .order_by(
            Transaction.pending_check_after.asc().nullsfirst(),
            Transaction.created_at,
            Transaction.id,
        )
        .with_for_update(of=Transaction, skip_locked=True)
        .limit(20)
    )
    for tx in due:
        ids.append(
            enqueue(
                s,
                "sync",
                "fetch_transaction",
                f"pending:{tx.id}:{int(cutoff.timestamp()) // 86400}",
                {"account_id": tx.account_id, "transaction_id": tx.id, "older_pending": True},
            )
        )
        tx.pending_check_after = cutoff + timedelta(days=1)
    if broad:
        due_names = s.scalars(
            select(Transaction)
            .join(Account)
            .where(
                Account.enabled.is_(True),
                Account.kind.in_(ACCOUNT_KINDS),
                Transaction.declined.is_(False),
                Transaction.identity_attempts < 3,
                ~CARD_CHECK,
                or_(
                    DISPLAY_MERCHANT == UNKNOWN, DISPLAY_MERCHANT.startswith("Payment reference: ")
                ),
                or_(
                    Transaction.identity_retry_after.is_(None),
                    Transaction.identity_retry_after <= cutoff,
                ),
            )
            .order_by(Transaction.identity_retry_after.asc().nullsfirst(), Transaction.id)
            .with_for_update(of=Transaction, skip_locked=True)
            .limit(20)
        )
        for tx in due_names:
            ids.append(
                enqueue(
                    s,
                    "sync",
                    "fetch_payment_identity",
                    f"identity:{tx.id}:{tx.identity_attempts}",
                    {"account_id": tx.account_id, "transaction_id": tx.id},
                )
            )
            tx.identity_attempts += 1
            tx.identity_retry_after = cutoff + timedelta(days=1)
    return ids
