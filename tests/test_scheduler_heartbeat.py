import pytest
from sqlalchemy import text

from monzo_backend.db import session
from monzo_backend.models import Connection


def heartbeats():
    with session() as s:
        return dict(
            s.execute(text("SELECT name, last_seen_at FROM monzo_scheduler_heartbeats")).all()
        )


@pytest.mark.parametrize("broad", [False, True])
def test_only_scheduled_automation_advances_heartbeat(
    client, owner_headers, automation_headers, broad
):
    payload = {"broad": broad, "scheduled": True}
    with session() as s:
        before = s.scalar(text("SELECT clock_timestamp()"))
    assert client.post("/v1/sync", headers=owner_headers, json=payload).status_code == 202
    assert not heartbeats()
    response = client.post("/v1/sync", headers=automation_headers, json=payload)
    assert response.status_code == 202
    beat = heartbeats()
    assert set(beat) == {"nightly" if broad else "recent"}
    assert next(iter(beat.values())) >= before
    for manual in ({"broad": broad}, {"broad": broad, "scheduled": False}):
        response = client.post("/v1/sync", headers=automation_headers, json=manual)
        assert response.status_code == 202
        assert response.json()["job_ids"]
        assert heartbeats() == beat


def test_heartbeat_survives_coalesced_jobs_but_not_failed_requests(
    client, automation_headers, monkeypatch
):
    import monzo_backend.api as api
    from monzo_backend.http import ServiceError

    assert (
        client.post("/v1/sync", headers=automation_headers, json={"scheduled": True}).status_code
        == 202
    )
    with session() as s, s.begin():
        old = s.scalar(
            text(
                "UPDATE monzo_scheduler_heartbeats SET last_seen_at = clock_timestamp() - interval '1 hour' RETURNING last_seen_at"
            )
        )
    assert (
        client.post("/v1/sync", headers=automation_headers, json={"scheduled": True}).status_code
        == 202
    )
    last = heartbeats()
    assert last["recent"] > old

    def fail(*args):
        raise ServiceError("synthetic", False)

    monkeypatch.setattr(api, "schedule_sync", fail)
    assert (
        client.post("/v1/sync", headers=automation_headers, json={"scheduled": True}).status_code
        == 502
    )
    assert heartbeats() == last


def test_scheduler_heartbeat_continues_while_waiting_for_reconnect(client, automation_headers):
    with session() as s, s.begin():
        s.get(Connection, 1).state = "reauthorisation_required"
    for broad in (False, True):
        response = client.post(
            "/v1/sync", headers=automation_headers, json={"broad": broad, "scheduled": True}
        )
        assert response.status_code == 202
        assert response.json() == {"job_ids": []}
    assert set(heartbeats()) == {"recent", "nightly"}
