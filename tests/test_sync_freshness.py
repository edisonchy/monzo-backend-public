from datetime import datetime, timedelta

from monzo_backend import monzo
from monzo_backend.db import session
from monzo_backend.health import snapshot
from monzo_backend.mcp.reporting import Reports
from monzo_backend.models import Account, now


def test_delayed_and_out_of_order_empty_windows(monkeypatch, client, owner_headers):
    monkeypatch.setattr(monzo, "bank_get", lambda *args: {"transactions": []})
    recent = now() - timedelta(hours=2)
    for end in [recent, recent - timedelta(hours=3)]:
        monzo.execute(
            {
                "id": "test-page",
                "kind": "sync_page",
                "payload": {
                    "account_id": "acc_test",
                    "since": (end - timedelta(days=1)).isoformat(),
                    "before": end.isoformat(),
                },
            }
        )
    with session() as s:
        data = snapshot(s)
        account = data["accounts"][0]
        assert account["state"] == "stale"
        assert account["checked_through"] == data["connection"]["checked_through"] == recent
        assert account["last_sync_at"] > recent
        assert (
            datetime.fromisoformat(Reports(s, None).metadata().coverage[0].checked_through)
            == recent
        )
    account = client.get("/v1/accounts", headers=owner_headers).json()[0]
    assert datetime.fromisoformat(account["checked_through"]) == recent


def test_finish_without_window_does_not_prove_freshness():
    with session() as s, s.begin():
        s.get(Account, "acc_test").last_sync_at = now()
    with session() as s:
        health = snapshot(s)
        assert health["accounts"][0]["state"] == "unknown"
        assert health["connection"]["checked_through"] is None
