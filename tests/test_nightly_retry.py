from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from monzo_backend import api, monzo
from monzo_backend.db import session
from monzo_backend.http import ServiceError
from monzo_backend.models import Connection, Job, NightlySyncReceipt, SchedulerHeartbeat, now

NIGHTLY = {"broad": True, "scheduled": True, "nightly_run_id": "123"}


def snapshot():
    with session() as s:
        return set(s.scalars(select(Job.id))), s.get(SchedulerHeartbeat, "nightly").last_seen_at


def test_lost_reply_replays_original_jobs_across_slots(client, automation_headers, monkeypatch):
    first_tick = now()
    monkeypatch.setattr(monzo, "now", lambda: first_tick)

    # The API commits and responds to the transport, but no HTTP bytes reach the caller.
    async def lose_reply(scope, receive, send):
        async def disconnected(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 202

        await api.app(scope, receive, disconnected)
        raise ConnectionResetError("connection closed before the client received a response")

    with pytest.raises(ConnectionResetError):
        TestClient(lose_reply).post("/v1/sync", headers=automation_headers, json=NIGHTLY)
    original = snapshot()
    with session() as s:
        page_ids = list(s.scalars(select(Job.id).where(Job.kind == "sync_page")))
    assert len(page_ids) == 1

    monkeypatch.setattr(monzo, "now", lambda: first_tick + timedelta(minutes=16))
    replay = client.post("/v1/sync", headers=automation_headers, json=NIGHTLY)
    assert replay.status_code == 202
    assert replay.json() == {"job_ids": page_ids}
    assert snapshot() == original
    next_tick = client.post(
        "/v1/sync", headers=automation_headers, json={**NIGHTLY, "nightly_run_id": "124"}
    )
    assert next_tick.status_code == 202
    assert next_tick.json() != replay.json()


def test_concurrent_duplicate_does_not_call_scheduler_twice(
    client, automation_headers, monkeypatch
):
    start = Barrier(2)
    calls = []
    real_schedule = api.schedule_sync

    def count_schedule(*args):
        calls.append(True)
        return real_schedule(*args)

    def attempt():
        start.wait(timeout=10)
        reply = client.post("/v1/sync", headers=automation_headers, json=NIGHTLY)
        assert reply.status_code == 202
        return reply.json()

    monkeypatch.setattr(api, "schedule_sync", count_schedule)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [pool.submit(attempt) for _ in range(2)]
        assert first.result() == second.result()
    assert len(calls) == 1


def test_retry_after_failed_scheduling_rolls_back_receipt_and_jobs(
    client, automation_headers, monkeypatch
):
    real_schedule = api.schedule_sync

    def fail_after_queueing(*args):
        real_schedule(*args)
        raise ServiceError("synthetic", True)

    monkeypatch.setattr(api, "schedule_sync", fail_after_queueing)
    assert client.post("/v1/sync", headers=automation_headers, json=NIGHTLY).status_code == 502
    with session() as s:
        assert not list(s.scalars(select(Job.id)))
        assert s.get(SchedulerHeartbeat, "nightly") is None
    monkeypatch.setattr(api, "schedule_sync", real_schedule)
    result = client.post("/v1/sync", headers=automation_headers, json=NIGHTLY)
    assert result.status_code == 202
    assert result.json()["job_ids"]


@pytest.mark.parametrize(
    ("identity", "changes"),
    [
        ("owner", {}),
        ("automation", {"broad": False}),
        ("automation", {"scheduled": False}),
        ("automation", {"nightly_run_id": "invalid"}),
    ],
)
def test_only_nightly_automation_can_claim_a_run(
    client, automation_headers, owner_headers, identity, changes
):
    headers = owner_headers if identity == "owner" else automation_headers
    response = client.post("/v1/sync", headers=headers, json={**NIGHTLY, **changes})
    assert response.status_code == 422
    with session() as s:
        assert not list(s.scalars(select(Job.id)))


def test_disconnected_empty_result_is_replayed_after_reconnect(client, automation_headers):
    with session() as s, s.begin():
        s.get(Connection, 1).state = "reauthorisation_required"
    assert client.post("/v1/sync", headers=automation_headers, json=NIGHTLY).json() == {
        "job_ids": []
    }
    first = snapshot()
    with session() as s, s.begin():
        s.get(Connection, 1).state = "healthy"
    assert client.post("/v1/sync", headers=automation_headers, json=NIGHTLY).json() == {
        "job_ids": []
    }
    assert snapshot() == first


def test_receipts_expire_outside_retry_window(client, automation_headers):
    with session() as s, s.begin():
        s.add(NightlySyncReceipt(run_id="100", recorded_at=now() - timedelta(days=8), job_ids=[]))
        s.add(NightlySyncReceipt(run_id="110", recorded_at=now() - timedelta(days=6), job_ids=[]))
    assert client.post("/v1/sync", headers=automation_headers, json=NIGHTLY).status_code == 202
    with session() as s:
        assert set(s.scalars(select(NightlySyncReceipt.run_id))) == {"110", "123"}
