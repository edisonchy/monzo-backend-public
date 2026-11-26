from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from monzo_backend import monzo, worker
from monzo_backend.db import session
from monzo_backend.domain import enqueue
from monzo_backend.models import Connection, Job, Notification, now


def test_reconnect_incident_alerts_once_and_scheduler_resumes(respx_mock):
    with session() as s, s.begin():
        conn = s.get(Connection, 1)
        conn.access_token, conn.expires_at = monzo.encrypt("old"), now() + timedelta(hours=1)
        for i in range(2):
            enqueue(s, "sync", "discover", f"bank-check-{i}", {})
    bank = respx_mock.get("https://api.monzo.com/accounts").mock(return_value=httpx.Response(401))
    assert worker.tick("sync")
    assert worker.tick("sync")
    with session() as s, s.begin():
        assert s.get(Connection, 1).state == "reauthorisation_required"
        assert monzo.schedule_sync(s) == []
        assert monzo.schedule_sync(s, broad=True) == []
        assert s.scalar(select(func.count()).select_from(Job)) == 2
        assert set(s.scalars(select(Job.state))) == {"failed"}
        assert len(s.scalars(select(Notification)).all()) == 1
    assert bank.call_count == 1

    respx_mock.get("https://api.monzo.com/ping/whoami").mock(
        return_value=httpx.Response(200, json={"authenticated": True, "user_id": "user_test"})
    )
    monzo.save_tokens({"access_token": "new", "expires_in": 3600}, "user_test")
    # New credentials get their own notice if the bank rejects them too.
    assert worker.tick("sync")
    with session() as s:
        notes = s.scalars(select(Notification)).all()
        assert len(notes) == 2
        assert all(note.event_key.startswith("reauth:") for note in notes)

    bank.mock(return_value=httpx.Response(200, json={"accounts": []}))
    monzo.save_tokens({"access_token": "working", "expires_in": 3600}, "user_test")
    assert worker.tick("sync")
    with session() as s, s.begin():
        assert s.get(Connection, 1).state == "healthy"
        assert monzo.schedule_sync(s)


def test_stale_401_retries_with_new_credentials(respx_mock):
    with session() as s, s.begin():
        conn = s.get(Connection, 1)
        conn.access_token, conn.expires_at = monzo.encrypt("old"), now() + timedelta(hours=1)
        job_id = enqueue(s, "sync", "discover", "discovery", {})

    def reconnected_while_request_in_flight(request):
        with session() as s, s.begin():
            s.get(Connection, 1).access_token = monzo.encrypt("new")
        return httpx.Response(401)

    respx_mock.get("https://api.monzo.com/accounts").mock(
        side_effect=reconnected_while_request_in_flight
    )
    assert worker.tick("sync")
    with session() as s:
        assert s.get(Job, job_id).state == "pending"
        assert s.get(Connection, 1).state == "healthy"
        assert s.scalar(select(func.count()).select_from(Notification)) == 0


@pytest.mark.parametrize("connection_state", ["healthy", "refreshing"])
def test_expired_credentials_create_one_incident_for_queued_work(connection_state):
    with session() as s, s.begin():
        conn = s.get(Connection, 1)
        conn.state, conn.expires_at = connection_state, now() - timedelta(minutes=1)
        for i in range(2):
            enqueue(s, "sync", "discover", f"queued-{i}", {})
    assert worker.tick("sync")
    assert worker.tick("sync")
    with session() as s:
        assert s.get(Connection, 1).state == "reauthorisation_required"
        assert s.scalars(select(Notification)).one().event_key.startswith("reauth:")
        assert set(s.scalars(select(Job.error_code))) == {"monzo_reauthorisation_required"}


def test_queued_unrelated_error_still_alerts_during_reconnect():
    with session() as s, s.begin():
        s.get(Connection, 1).state = "reauthorisation_required"
        job_id = enqueue(s, "sync", "unknown_job", "bad-job", {})
    assert worker.tick("sync")
    with session() as s:
        assert s.scalars(select(Notification)).one().event_key == f"sync-failed:{job_id}"
