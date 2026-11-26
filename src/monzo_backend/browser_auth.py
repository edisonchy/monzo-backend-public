"""Private browser sessions; owner API credentials never enter the frontend."""

import hashlib
import hmac
import ipaddress
import secrets
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, SecretStr
from sqlalchemy import case, delete, func, text
from sqlalchemy.dialects.postgresql import insert

from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.models import BrowserSession, LoginBucket, now

router = APIRouter(prefix="/dashboard")
COOKIE = "__Host-monzo_session"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + key.hex()


def same_origin(request):
    origin = settings().dashboard_origin
    if (
        not origin
        or request.headers.get("origin") != origin
        or request.headers.get("x-dashboard-request") != "1"
    ):
        raise HTTPException(403, "Same-origin dashboard request required")


def browser_identity(request):
    cfg = settings()
    token = request.cookies.get(COOKIE, "")
    if not token or not cfg.dashboard_origin or not cfg.dashboard_password_hash.get_secret_value():
        raise HTTPException(401, "Please sign in")
    with session() as s:
        row = s.get(BrowserSession, digest(token))
        if (
            not row
            or row.expires_at <= now()
            or row.credential_tag != digest(cfg.dashboard_password_hash.get_secret_value())
        ):
            raise HTTPException(401, "Session expired; please sign in")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        same_origin(request)
    return "owner"


def login_client_key(request):
    cfg = settings()
    peer = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        address = None
    if address in cfg.dashboard_trusted_proxy_ips:
        # Only the pinned gateway peers may supply this overwritten header.
        try:
            address = ipaddress.ip_address(request.headers.get("x-monzo-client-ip", ""))
        except ValueError:
            raise HTTPException(400, "Invalid gateway client address") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    identity = str(address) if address is not None else "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        identity = str(ipaddress.ip_network(f"{address}/64", strict=False))
    return hmac.new(
        cfg.dashboard_password_hash.get_secret_value().encode(), identity.encode(), hashlib.sha256
    ).hexdigest()


class Login(BaseModel):
    password: SecretStr


@router.post("/login")
def login(request: Request, response: Response, body: Login):
    same_origin(request)
    cfg = settings()
    encoded = cfg.dashboard_password_hash.get_secret_value()
    if not encoded:
        raise HTTPException(503, "Dashboard login is not configured")
    # Each gateway-authenticated client gets its own durable, atomic allowance.
    key = login_client_key(request)
    stamp = now()
    expired = LoginBucket.started_at <= stamp - timedelta(minutes=1)
    with session() as s, s.begin():
        attempts = s.scalar(
            insert(LoginBucket)
            .values(client_key=key, started_at=stamp, attempts=1)
            .on_conflict_do_update(
                index_elements=[LoginBucket.client_key],
                set_={
                    "started_at": case((expired, stamp), else_=LoginBucket.started_at),
                    "attempts": case((expired, 1), else_=func.least(LoginBucket.attempts + 1, 11)),
                },
            )
            .returning(LoginBucket.attempts)
        )
        # Bound storage and work; materialization prevents LIMIT rescans during DELETE.
        s.execute(
            text("""WITH expired AS MATERIALIZED (
            SELECT client_key FROM browser_login_buckets
            WHERE started_at < :cutoff ORDER BY started_at, client_key
            LIMIT 100 FOR UPDATE SKIP LOCKED
        ) DELETE FROM browser_login_buckets WHERE client_key IN (SELECT client_key FROM expired)
        """),
            {"cutoff": stamp - timedelta(hours=1)},
        )
    blocked = attempts > 10
    if blocked:
        raise HTTPException(
            429, "Too many attempts; try again in one minute", headers={"Retry-After": "60"}
        )
    password = body.password.get_secret_value()
    valid = False
    if len(password) <= 512:
        try:
            valid = secrets.compare_digest(hash_password(password, encoded.split(":")[0]), encoded)
        except ValueError:
            pass
    if not valid:
        raise HTTPException(401, "Incorrect password")
    token = secrets.token_urlsafe(32)
    with session() as s, s.begin():
        s.execute(delete(BrowserSession).where(BrowserSession.expires_at <= now()))
        old = request.cookies.get(COOKIE)
        if old:
            s.execute(delete(BrowserSession).where(BrowserSession.token_hash == digest(old)))
        s.add(
            BrowserSession(
                token_hash=digest(token),
                credential_tag=digest(encoded),
                expires_at=now() + timedelta(hours=8),
            )
        )
    response.set_cookie(
        COOKIE, token, max_age=28800, httponly=True, secure=True, samesite="strict", path="/"
    )
    return {"authenticated": True}


@router.get("/session")
def whoami(request: Request):
    browser_identity(request)
    return {"authenticated": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    same_origin(request)
    with session() as s, s.begin():
        s.execute(
            delete(BrowserSession).where(
                BrowserSession.token_hash == digest(request.cookies.get(COOKIE, ""))
            )
        )
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return {"authenticated": False}
