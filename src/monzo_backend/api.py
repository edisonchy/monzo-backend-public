import secrets
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, StrictBool
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from monzo_backend import bank_oauth, budgets, custom_categories, health
from monzo_backend.browser_auth import browser_identity
from monzo_backend.browser_auth import router as browser_router
from monzo_backend.card_checks import CARD_CHECK, is_card_check
from monzo_backend.config import settings
from monzo_backend.coverage import accounts_with_cutoff
from monzo_backend.dashboard import make_router
from monzo_backend.db import session
from monzo_backend.domain import control, digest, enqueue, pause, resume
from monzo_backend.http import ServiceError
from monzo_backend.live import router as live_router
from monzo_backend.models import (
    Account,
    Audit,
    Connection,
    Control,
    Job,
    MerchantResearch,
    NightlySyncReceipt,
    Notification,
    Rule,
    SchedulerHeartbeat,
    Transaction,
    WebhookEvent,
    now,
    uid,
)
from monzo_backend.monzo import save_tokens, schedule_sync


@asynccontextmanager
async def lifespan(_app):
    settings()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Monzo Backend",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
bearer = HTTPBearer(auto_error=False)
app.include_router(live_router)


def actor(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials:
        for name, token in (
            ("owner", settings().owner_token),
            ("automation", settings().automation_token),
        ):
            if secrets.compare_digest(credentials.credentials, token.get_secret_value()):
                return name
    if not credentials and request.cookies.get("__Host-monzo_session"):
        return browser_identity(request)
    raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def owner(identity=Depends(actor)):
    if identity != "owner":
        raise HTTPException(403, "Owner access required")


@app.middleware("http")
async def limit_body(request: Request, call_next):
    size, body = 0, []
    async for chunk in request.stream():
        size += len(chunk)
        if size > 65536:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        body.append(chunk)
    request._body = b"".join(body)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.exception_handler(ServiceError)
async def service_error(request, exc):
    return JSONResponse({"detail": exc.code}, status_code=502)


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    return JSONResponse({"detail": "Data service unavailable. Try again shortly."}, status_code=503)


@app.get("/health/live")
def live():
    return {"status": "alive"}


@app.get("/health/ready")
def ready():
    try:
        with session() as s:
            if not s.get(Control, 1):
                raise HTTPException(503, "Database migration required")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ready"}


@app.get("/v1/status", dependencies=[Depends(actor)])
def status():
    with session() as s:
        return health.snapshot(s)


@app.get("/v1/openapi", dependencies=[Depends(owner)])
def schema():
    return app.openapi()


@app.get("/v1/accounts", dependencies=[Depends(owner)])
def accounts():
    with session() as s:
        return [
            {
                "id": a.id,
                "kind": a.kind,
                "enabled": a.enabled,
                "history_status": a.history_status,
                "last_sync_at": a.last_sync_at,
                "checked_through": cutoff,
            }
            for a, cutoff in s.execute(accounts_with_cutoff())
        ]


class SyncRequest(BaseModel):
    broad: bool = False
    scheduled: StrictBool = False
    nightly_run_id: str | None = Field(default=None, pattern=r"^[0-9]+$", max_length=32)


@app.post("/v1/sync", status_code=202)
def sync(body: SyncRequest, identity=Depends(actor)):
    scheduled = identity == "automation" and body.scheduled
    if body.nightly_run_id is not None and not (scheduled and body.broad):
        raise HTTPException(422, "nightly_run_id is only valid for scheduled nightly automation")
    with session() as s, s.begin():
        receipt = None
        if body.nightly_run_id is not None:
            # Keep a week of receipts for the bounded HTTP retries, including across a slot boundary.
            s.execute(
                delete(NightlySyncReceipt).where(
                    NightlySyncReceipt.recorded_at < func.now() - timedelta(days=7)
                )
            )
            receipt = s.scalar(
                insert(NightlySyncReceipt)
                .values(run_id=body.nightly_run_id, job_ids=[])
                .on_conflict_do_nothing(index_elements=[NightlySyncReceipt.run_id])
                .returning(NightlySyncReceipt)
            )
            # A concurrent insert waits for the original transaction to commit or roll back.
            if receipt is None:
                previous = s.get_one(NightlySyncReceipt, body.nightly_run_id)
                return {"job_ids": previous.job_ids}
        jobs = schedule_sync(s, body.broad)
        if receipt is not None:
            receipt.job_ids = jobs
        if scheduled:
            # Record delivery even if this tick reused a job or the bank needs reconnection.
            stamp = insert(SchedulerHeartbeat).values(
                name="nightly" if body.broad else "recent", last_seen_at=func.clock_timestamp()
            )
            s.execute(
                stamp.on_conflict_do_update(
                    index_elements=[SchedulerHeartbeat.name],
                    set_={
                        "last_seen_at": func.greatest(
                            SchedulerHeartbeat.last_seen_at, stamp.excluded.last_seen_at
                        )
                    },
                )
            )
        return {"job_ids": jobs}


@app.post("/v1/connection/discover", dependencies=[Depends(owner)], status_code=202)
def discover():
    with session() as s, s.begin():
        if not s.get(Connection, 1):
            raise HTTPException(409, "Connect Monzo first")
        return {
            "job_id": enqueue(
                s,
                "sync",
                "discover",
                f"discover-owner:{int(now().timestamp()) // 60}",
                {"reconnect": True},
            )
        }


class TokenImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    user_id: str
    expires_in: int = Field(default=60, ge=0, le=86400)


@app.post("/v1/connection/import", dependencies=[Depends(owner)], status_code=202)
def import_connection(body: TokenImport):
    save_tokens(
        {
            "access_token": body.access_token.get_secret_value(),
            "refresh_token": body.refresh_token.get_secret_value() if body.refresh_token else None,
            "expires_in": body.expires_in,
        },
        expected_user=body.user_id,
    )
    return {"status": "discovery_queued"}


class WebhookData(BaseModel):
    id: str = Field(pattern=r"^tx_[A-Za-z0-9]+$", max_length=128)
    account_id: str = Field(pattern=r"^acc_[A-Za-z0-9]+$", max_length=128)


class Webhook(BaseModel):
    type: str
    data: WebhookData


@app.post("/webhooks/monzo/{secret}", status_code=202)
def webhook(secret: str, body: Webhook, request: Request):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "JSON required")
    if not secrets.compare_digest(secret, settings().webhook_secret.get_secret_value()):
        raise HTTPException(404, "Not found")
    if body.type != "transaction.created":
        return {"accepted": False}
    with session() as s, s.begin():
        account = s.get(Account, body.data.account_id)
        if not account or not account.enabled:
            raise HTTPException(404, "Unknown account")
        fingerprint = digest(body.model_dump())
        s.execute(
            insert(WebhookEvent)
            .values(
                id=uid(),
                transaction_id=body.data.id,
                account_id=body.data.account_id,
                payload_hash=fingerprint,
            )
            .on_conflict_do_nothing()
        )
        job_id = enqueue(
            s,
            "sync",
            "fetch_transaction",
            f"webhook:{body.data.id}:{fingerprint}",
            {"transaction_id": body.data.id, "account_id": body.data.account_id},
        )
        job = s.get(Job, job_id, with_for_update=True)
        if job.state == "failed" and "webhook_retry_limit" not in job.payload:
            # Persist the one extra attempt so concurrent or later deliveries cannot renew it.
            job.payload = {**job.payload, "webhook_retry_limit": job.attempts + 1}
            job.state, job.available_at, job.error_code = "pending", now(), None
    return {"accepted": True}


@app.post("/v1/processing/resume", dependencies=[Depends(owner)])
def resume_processing():
    with session() as s, s.begin():
        return {"state": resume(s)}


@app.post("/v1/processing/pause", dependencies=[Depends(owner)])
def pause_processing():
    with session() as s, s.begin():
        ctl = control(s)
        if ctl.state != "paused":
            job = s.get(Job, ctl.active_job_id) if ctl.active_job_id else None
            pause(s, ctl, job, "owner_requested_pause")
            s.add(Audit(actor="owner", action="pause_requested"))
        return {"state": ctl.state}


@app.get("/v1/categories", dependencies=[Depends(owner)])
def categories():
    with session() as s:
        return {"version": "v1", "categories": custom_categories.keys(s)}


@app.post("/v1/reclassify", dependencies=[Depends(owner)], status_code=202)
def reclassify(limit: int = Query(100, ge=1, le=100), unprocessed_only: bool = False):
    with session() as s, s.begin():
        query = select(Transaction).where(
            Transaction.category_status == "needs_review",
            ~CARD_CHECK,
            or_(Transaction.category_source.is_(None), Transaction.category_source != "manual"),
        )
        if unprocessed_only:
            query = query.where(
                func.length(func.trim(Transaction.merchant_name)) > 0,
                or_(Transaction.category_source.is_(None), Transaction.category_source == "review"),
            )
        rows = s.scalars(
            query.order_by(Transaction.created_at, Transaction.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for tx in rows:
            tx.version += 1
            tx.category, tx.category_status = None, "pending"
            enqueue(
                s,
                "category",
                "classify",
                f"category:{tx.id}:{tx.version}:v1",
                {"transaction_id": tx.id, "version": tx.version},
            )
        s.add(
            Audit(
                actor="owner",
                action="review_reclassification_requested",
                detail={"count": len(rows)},
            )
        )
        return {"queued": len(rows)}


@app.get("/v1/transactions", dependencies=[Depends(owner)])
def transactions(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    review_only: bool = False,
    account_id: str | None = None,
):
    with session() as s:
        query = select(Transaction)
        if review_only:
            query = query.where(Transaction.category_status == "needs_review", ~CARD_CHECK)
        if account_id:
            query = query.where(Transaction.account_id == account_id)
        rows = s.scalars(
            query.order_by(Transaction.created_at.desc(), Transaction.id)
            .limit(limit)
            .offset(offset)
        )
        return [
            {
                "id": t.id,
                "account_id": t.account_id,
                "amount_minor": t.amount,
                "currency": t.currency,
                "created_at": t.created_at,
                "settled_at": t.settled_at,
                "declined": t.declined,
                "merchant": t.merchant_name,
                "bank_category": t.bank_category,
                "category": t.category,
                "category_status": t.category_status,
                "category_source": t.category_source,
                "version": t.version,
                "card_verification": is_card_check(t),
            }
            for t in rows
        ]


@app.get("/v1/cashflow", dependencies=[Depends(owner)])
def cashflow(start: AwareDatetime, end: AwareDatetime, settled_only: bool = True):
    if end <= start or (end - start).days > 366:
        raise HTTPException(422, "Use an increasing range of at most 366 days")
    effective_category = case(
        (Transaction.category_status == "assigned", Transaction.category), else_="uncategorised"
    )
    with session() as s:
        query = select(
            Transaction.currency,
            effective_category.label("category"),
            func.count(),
            func.sum(case((Transaction.amount < 0, Transaction.amount), else_=0)),
            func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)),
        ).where(
            Transaction.created_at >= start,
            Transaction.created_at < end,
            Transaction.declined.is_(False),
            ~CARD_CHECK,
        )
        if settled_only:
            query = query.where(Transaction.settled_at.is_not(None))
        rows = s.execute(query.group_by(Transaction.currency, effective_category)).all()
        return {
            "basis": "transaction_created_at",
            "settled_only": settled_only,
            "interpretation": "Gross account movements; transfers are included. Credits are not necessarily income.",
            "categorisation_state": s.get(Control, 1).state,
            "groups": [
                {
                    "currency": r[0],
                    "category": r[1],
                    "count": r[2],
                    "debits_minor": -int(r[3]),
                    "credits_minor": int(r[4]),
                }
                for r in rows
            ],
        }


class Correction(BaseModel):
    category: str
    expected_version: int


@app.put("/v1/transactions/{tx_id}/category", dependencies=[Depends(owner)])
def correct(tx_id: str, body: Correction):
    with session() as s, s.begin():
        if not custom_categories.known(s, body.category):
            raise HTTPException(422, "Unknown category")
        tx = s.scalar(select(Transaction).where(Transaction.id == tx_id).with_for_update())
        if not tx:
            raise HTTPException(404, "Transaction not found")
        if is_card_check(tx):
            raise HTTPException(409, "Card verifications do not need a spending category")
        if tx.version != body.expected_version:
            raise HTTPException(409, "Transaction changed; reload before correcting")
        s.add(
            Audit(
                actor="owner",
                action="category_corrected",
                detail={
                    "transaction_id": tx.id,
                    "previous_category": tx.category,
                    "category": body.category,
                },
            )
        )
        tx.category, tx.category_source, tx.category_status = body.category, "manual", "assigned"
        tx.version += 1
        return {"id": tx.id, "category": tx.category, "version": tx.version}


class RuleRequest(BaseModel):
    merchant_id: str = Field(max_length=128)
    category: str


@app.put("/v1/rules", dependencies=[Depends(owner)])
def put_rule(body: RuleRequest):
    with session() as s, s.begin():
        if not custom_categories.known(s, body.category):
            raise HTTPException(422, "Unknown category")
        s.execute(
            insert(Rule)
            .values(id=uid(), effective_from=now(), **body.model_dump())
            .on_conflict_do_update(
                index_elements=[Rule.merchant_id],
                set_={"category": body.category, "effective_from": now()},
            )
        )
        s.add(Audit(actor="owner", action="merchant_rule_saved", detail=body.model_dump()))
    return {"status": "saved", "scope": "future_classifications"}


@app.post("/v1/notifications/claim", dependencies=[Depends(actor)])
def claim_notification():
    with session() as s, s.begin():
        note = s.scalar(
            select(Notification)
            .where(
                Notification.delivered_at.is_(None),
                Notification.available_at <= now(),
                or_(Notification.lease_until.is_(None), Notification.lease_until < now()),
            )
            .order_by(Notification.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not note:
            return {"notification": None}
        note.lease_token, note.lease_until = uid(), now() + timedelta(minutes=2)
        note.attempts += 1
        note.available_at = now() + timedelta(seconds=min(3600, 30 * 2 ** min(note.attempts, 7)))
        return {
            "notification": {
                "id": note.id,
                "lease_token": note.lease_token,
                "subject": note.subject,
                "body": note.body,
                "event_key": note.event_key,
            }
        }


class Ack(BaseModel):
    lease_token: str


@app.post("/v1/notifications/{note_id}/ack", dependencies=[Depends(actor)])
def acknowledge(note_id: str, body: Ack):
    with session() as s, s.begin():
        note = s.scalar(select(Notification).where(Notification.id == note_id).with_for_update())
        if not note:
            raise HTTPException(404, "Notification not found")
        if note.lease_token != body.lease_token:
            raise HTTPException(409, "Delivery lease changed")
        note.delivered_at = note.delivered_at or now()
        return {"status": "delivered"}


@app.get("/v1/merchant-research", dependencies=[Depends(owner)])
def merchant_research(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    with session() as s:
        rows = s.scalars(
            select(MerchantResearch)
            .order_by(MerchantResearch.created_at, MerchantResearch.key)
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "findings": [
                {
                    "key": r.key,
                    "merchant": r.merchant_name,
                    "finding": r.finding,
                    "model": r.model,
                    "researched_at": r.created_at,
                }
                for r in rows
            ]
        }


app.include_router(browser_router)

app.include_router(make_router(owner))
app.include_router(budgets.make_router(owner))

app.include_router(bank_oauth.make_router(owner))
