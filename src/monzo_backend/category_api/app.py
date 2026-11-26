"""Private HTTPS-only command listener. Deliberately not the dashboard app."""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from monzo_backend import category_operations as ops
from monzo_backend.category_changes import CategoryChoice
from monzo_backend.domain import CATEGORIES
from monzo_backend.mcp.contracts import CategoryBatchInput
from monzo_backend.mcp.security import AuthFailure
from monzo_backend.models import CategoryWriteControl

from .database import command_engine, sessions
from .limits import RateLimited, admit
from .security import READ, WRITE, CommandSettings, CommandVerifier

log = logging.getLogger("monzo.category_api")
PREFIX = "/internal/categories"
MAX_BODY = 65536
DEADLINE = 10.0


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChangeInput(EmptyInput):
    transaction_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    category: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)


class UndoInput(EmptyInput):
    batch_id: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def response(data, status=200, headers=None):
    payload = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **(headers or {})}
    # These payloads contain only bounded service results, never driver details.
    return JSONResponse(data, status_code=status, headers=payload)


def failure(code, status, **kwargs):
    return response({"error": code}, status, **kwargs)


def execute(action, actor, body, operation_id, engine, deadline, batches_enabled=False):
    factory = sessions(engine, deadline)
    if action == "options":
        with factory() as s:
            enabled = bool(
                s.scalar(select(CategoryWriteControl.enabled).where(CategoryWriteControl.id == 1))
            )
        return {
            "categories": [
                {"id": c, "label": c.replace("_", " ").capitalize()} for c in CATEGORIES
            ],
            "writes_enabled": enabled,
            "scope": "this",
            "future_rules": batches_enabled,
            "batch_scopes": ["unverified", "all"] if batches_enabled else [],
            "batch_limit": 100 if batches_enabled else 0,
            "group_scope": "unverified",
        }, 200
    if action == "get":
        with factory() as s:
            return ops.get_operation(s, actor, operation_id), 200
    admit(factory, actor, "commit" if action in {"apply", "undo"} else "preview")
    if action in {"apply", "undo"}:
        result = ops.commit_operation(
            actor, operation_id, action, session_factory=factory, allow_batches=batches_enabled
        )
        return result, 409 if result["state"] in {"expired", "stale"} else 200
    with factory() as s, s.begin():
        if action == "preview":
            result = ops.prepare_change(
                s,
                actor,
                body.transaction_id,
                CategoryChoice(category=body.category, expected_version=body.expected_version),
            )
        elif action == "batch_preview":
            if not batches_enabled:
                raise ops.OperationError("batches_disabled")
            result = ops.prepare_batch(
                s,
                actor,
                body.transaction_id,
                body.merchant_id,
                body.merchant_group_id,
                CategoryChoice(
                    category=body.category,
                    expected_version=body.expected_version,
                    scope=body.scope,
                    future_rule=body.future_rule,
                    group_merchant=bool(body.merchant_group_id and body.scope != "this"),
                ),
            )
        else:
            result = ops.prepare_undo(s, actor, body.batch_id, allow_batches=batches_enabled)
    return result, 201


class CommandApp:
    def __init__(self, settings, engine, verifier):
        self.settings, self.engine, self.verifier = settings, engine, verifier
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="category-command")
        self.active = set()

        @asynccontextmanager
        async def lifespan(_):
            yield
            # Threads retain their slot until the transaction actually finishes.
            self.pool.shutdown(wait=True, cancel_futures=True)
            if self.engine:
                self.engine.dispose()

        def endpoint(action, model):
            async def handle(request):
                return await self.handle(request, action, model)

            return handle

        self.router = Starlette(
            lifespan=lifespan,
            routes=[
                Route(PREFIX + "/options", endpoint("options", None), methods=["GET"]),
                Route(PREFIX + "/previews", endpoint("preview", ChangeInput), methods=["POST"]),
                Route(
                    PREFIX + "/batch-previews",
                    endpoint("batch_preview", CategoryBatchInput),
                    methods=["POST"],
                ),
                Route(
                    PREFIX + "/undo-previews", endpoint("preview_undo", UndoInput), methods=["POST"]
                ),
                Route(
                    PREFIX + "/operations/{operation_id}", endpoint("get", None), methods=["GET"]
                ),
                Route(
                    PREFIX + "/operations/{operation_id}/apply",
                    endpoint("apply", EmptyInput),
                    methods=["POST"],
                ),
                Route(
                    PREFIX + "/operations/{operation_id}/undo",
                    endpoint("undo", EmptyInput),
                    methods=["POST"],
                ),
            ],
        )

        self.router.router.redirect_slashes = False

    async def handle(self, request: Request, action, model):
        mutation = action not in {"get", "options"}
        try:
            actor, claims = await self.verifier.authorize(
                request.headers.get("authorization"), write=action != "options"
            )
            if request.query_params:
                return failure("invalid_request", 422)
            body = None
            if model:
                if (
                    request.headers.get("content-type", "").split(";")[0].strip().lower()
                    != "application/json"
                ):
                    return failure("json_required", 415)
                length = request.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > MAX_BODY):
                    return failure("request_too_large", 413)
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY:
                        return failure("request_too_large", 413)
                try:
                    body = model.model_validate(json.loads(raw))
                except (ValidationError, ValueError, UnicodeDecodeError):
                    return failure("invalid_request", 422)
            elif request.headers.get("content-length", "0") != "0" or request.headers.get(
                "transfer-encoding"
            ):
                return failure("invalid_request", 422)
            # Bound running and queued work. A timed-out HTTP request never frees
            # a slot while its SQL thread is still executing or rolling back.
            if len(self.active) >= 2:
                return failure("busy", 503, headers={"Retry-After": "1"})
            remaining = claims["exp"] - time.time()
            if remaining <= 0:
                raise AuthFailure(401)
            deadline = min(request.scope["category_deadline"], time.monotonic() + remaining)
            future = self.pool.submit(
                execute,
                action,
                actor,
                body,
                request.path_params.get("operation_id"),
                self.engine,
                deadline,
                self.settings.batches_enabled,
            )
            self.active.add(future)
            loop = asyncio.get_running_loop()
            future.add_done_callback(
                lambda done: loop.call_soon_threadsafe(self.active.discard, done)
            )
            wrapped = asyncio.wrap_future(future)
            wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            data, status = await asyncio.shield(wrapped)
            if len(json.dumps(data).encode()) > MAX_BODY:
                return failure("outcome_unknown" if mutation else "result_too_large", 503)
            if action == "options":
                data["can_write"] = data["writes_enabled"] and WRITE in claims["scope"].split()
            return response(data, status)
        except AuthFailure as exc:
            code = "invalid_token" if exc.status == 401 else "insufficient_scope_or_identity"
            challenge = f'Bearer error="{"invalid_token" if exc.status == 401 else "insufficient_scope"}", scope="{READ} {WRITE}"'
            return failure(code, exc.status, headers={"WWW-Authenticate": challenge})
        except RateLimited as exc:
            return failure("rate_limited", 429, headers={"Retry-After": str(exc.retry_after)})
        except ops.OperationError as exc:
            status = (
                404
                if exc.code in {"operation_not_found", "batch_not_found"}
                else 503
                if exc.code in {"writes_disabled", "control_missing", "deadline_exceeded"}
                else 409
            )
            code = "outcome_unknown" if mutation and exc.code == "deadline_exceeded" else exc.code
            return failure(code, status)
        except HTTPException as exc:
            code = (
                "not_found"
                if exc.status_code == 404
                else "invalid_request"
                if exc.status_code == 422
                else "already_undone"
                if "already undone" in str(exc.detail)
                else "undo_conflict"
                if "undo" in action
                else "stale_preview"
            )
            return failure(code, exc.status_code)
        except SQLAlchemyError as exc:
            state = getattr(getattr(exc, "orig", None), "sqlstate", None)
            code = (
                "busy"
                if state in {"55P03", "40P01", "40001"}
                else "outcome_unknown"
                if mutation
                else "service_unavailable"
            )
            log.warning("category_command_failed action=%s code=%s", action, code)
            return failure(code, 503)
        except Exception:
            # A post-commit response failure is not evidence that the edit failed.
            log.warning("category_command_failed action=%s code=unexpected", action)
            return failure("outcome_unknown" if mutation else "service_unavailable", 503)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.router(scope, receive, send)
        headers = scope.get("headers", [])
        hosts = [v.decode("latin-1") for k, v in headers if k.lower() == b"host"]
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        if scope.get("scheme") != "https" or hosts not in [["category-api"], ["category-api:8443"]]:
            return await failure("private_https_required", 400)(scope, receive, send)
        if len(auth) > 1:
            return await failure("invalid_token", 401)(scope, receive, send)
        if scope["path"] == "/health" and scope["method"] == "GET":
            return await response({"status": "alive"})(scope, receive, send)
        if not self.settings.enabled:
            return await failure("service_disabled", 503)(scope, receive, send)
        # Buffer output until successful completion so timeouts cannot return a
        # second response after headers have already reached the caller.
        outgoing = []

        async def capture(message):
            outgoing.append(message)

        scope["category_deadline"] = time.monotonic() + DEADLINE
        try:
            async with asyncio.timeout(DEADLINE):
                await self.router(scope, receive, capture)
        except TimeoutError:
            mutation = scope["method"] == "POST"
            return await failure("outcome_unknown" if mutation else "deadline_exceeded", 503)(
                scope, receive, send
            )
        for message in outgoing:
            await send(message)


def create_app(settings=None, *, engine=None, verifier=None):
    settings = settings or CommandSettings()
    engine = engine or (
        command_engine(settings.database_url.get_secret_value()) if settings.enabled else None
    )
    return CommandApp(settings, engine, verifier or CommandVerifier(settings))
