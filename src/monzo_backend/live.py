"""Cookie-authenticated invalidations, never financial payloads or bank polling."""

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from monzo_backend.browser_auth import browser_identity
from monzo_backend.db import session
from monzo_backend.models import DashboardRevision

router = APIRouter(prefix="/v1/dashboard")
# The deployment runs one API process. Each stream releases its DB connection
# between reads; a cap prevents idle browser tabs exhausting server resources.
streams = 0
MAX_STREAMS = 12
POLL_SECONDS = 2
HEARTBEAT_SECONDS = 20


def snapshot(request):
    browser_identity(request)
    with session() as s:
        row = s.get(DashboardRevision, 1)
        return {
            "revision": row.revision if row else 0,
            "changed_at": row.changed_at.isoformat() if row else None,
        }


@router.get("/revision")
def revision(request: Request):
    return snapshot(request)


async def events(request):
    previous, heartbeat = None, time.monotonic()
    try:
        while not await request.is_disconnected():
            try:
                current = await run_in_threadpool(snapshot, request)
            except HTTPException:
                yield "event: expired\ndata: {}\n\n"
                return
            if current["revision"] != previous:
                yield f"event: revision\ndata: {json.dumps(current)}\n\n"
                previous = current["revision"]
            if time.monotonic() - heartbeat >= HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
    finally:
        global streams
        streams -= 1


@router.get("/events")
async def stream(request: Request):
    await run_in_threadpool(browser_identity, request)
    global streams
    if streams >= MAX_STREAMS:
        raise HTTPException(429, "Too many live connections", headers={"Retry-After": "60"})
    streams += 1
    return StreamingResponse(
        events(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
