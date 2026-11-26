"""Owner-initiated bank consent, independent of dashboard and MCP sign-in."""

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select, text, update

from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.health import oauth_configured
from monzo_backend.http import ServiceError
from monzo_backend.http import request as upstream
from monzo_backend.models import OAuthState, now
from monzo_backend.monzo import save_tokens


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def make_router(owner):
    router = APIRouter()

    @router.post("/v1/oauth/start", dependencies=[Depends(owner)])
    def start(request: Request, response: Response):
        cfg = settings()
        if not cfg.client_id or not cfg.client_secret.get_secret_value():
            raise HTTPException(409, "Monzo OAuth client is not configured")
        if request.cookies.get("__Host-monzo_session") and not oauth_configured():
            raise HTTPException(
                409, "Configure the dashboard callback in Monzo developer settings first"
            )
        state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with session() as s, s.begin():
            s.execute(text("SELECT pg_advisory_xact_lock(712003)"))
            s.execute(delete(OAuthState).where(OAuthState.expires_at < now() - timedelta(days=1)))
            s.execute(
                update(OAuthState)
                .where(OAuthState.outcome.in_(("pending", "exchanging")))
                .values(used=True, outcome="cancelled")
            )
            s.add(
                OAuthState(
                    state_hash=sha(state),
                    binding_hash=sha(binding),
                    expires_at=now() + timedelta(minutes=10),
                )
            )
        response.set_cookie(
            "monzo_oauth_binding",
            binding,
            httponly=True,
            secure=cfg.secure_cookies,
            samesite="lax",
            max_age=600,
            path="/oauth",
        )
        return {
            "authorization_url": "https://auth.monzo.com/?"
            + urlencode(
                {
                    "client_id": cfg.client_id,
                    "redirect_uri": cfg.redirect_uri,
                    "response_type": "code",
                    "state": state,
                }
            )
        }

    @router.get("/oauth/callback")
    def callback(
        request: Request,
        state: str = Query(max_length=200),
        code: str | None = Query(None, max_length=2000),
        error: str | None = Query(None, max_length=200),
    ):
        binding = request.cookies.get("monzo_oauth_binding", "")
        with session() as s, s.begin():
            row = s.scalar(
                select(OAuthState).where(OAuthState.state_hash == sha(state)).with_for_update()
            )
            if (
                not row
                or row.used
                or row.expires_at <= now()
                or not secrets.compare_digest(row.binding_hash, sha(binding))
            ):
                raise HTTPException(
                    400, "Invalid or expired OAuth session. Return to Settings to start again."
                )
            row.used = True
            row.outcome = "failed" if error or not code else "exchanging"
        if code and not error:
            cfg = settings()
            try:
                tokens = upstream(
                    "POST",
                    cfg.api_url + "/oauth2/token",
                    service="monzo",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": cfg.redirect_uri,
                        "client_id": cfg.client_id,
                        "client_secret": cfg.client_secret.get_secret_value(),
                    },
                )
                save_tokens(tokens, oauth_state_hash=sha(state))
            except ServiceError:
                with session() as s, s.begin():
                    s.execute(
                        update(OAuthState)
                        .where(
                            OAuthState.state_hash == sha(state), OAuthState.outcome == "exchanging"
                        )
                        .values(outcome="failed")
                    )
        # Fixed destination: neither upstream error text nor a return URL is reflected.
        result = (
            RedirectResponse("/settings", status_code=303)
            if oauth_configured()
            else JSONResponse(
                {
                    "status": "check_connection",
                    "action": "Approve access in Monzo, then check connection status.",
                }
            )
        )
        with session() as s:
            latest = s.scalar(select(OAuthState).order_by(OAuthState.expires_at.desc()).limit(1))
            if latest and latest.state_hash == sha(state):
                result.delete_cookie("monzo_oauth_binding", path="/oauth")
        return result

    return router
