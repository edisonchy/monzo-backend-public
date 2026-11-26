"""Durable metadata for each application-to-provider request, including failures."""

import time

from monzo_backend.db import session
from monzo_backend.http import ServiceError
from monzo_backend.models import Audit


def tracked_request(send, operation, work, *args, **kwargs):
    detail = {
        "operation": operation,
        "state": "started",
        "job_id": (work or {}).get("id"),
        "input_version": (work or {}).get("payload", {}).get("version"),
        "recovery_probe": bool((work or {}).get("recovering")),
    }
    with session() as s, s.begin():
        record = Audit(actor="worker", action="ai_provider_request", detail=detail)
        s.add(record)
        s.flush()
        record_id = record.id
    started = time.monotonic()
    try:
        result = send(*args, **kwargs)
        # Receipt is distinct from successful validation of the model's output.
        detail = {**detail, "state": "response_received"}
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        detail["usage"] = (
            {
                k: v
                for k, v in usage.items()
                if k
                in {
                    "prompt_tokens",
                    "completion_tokens",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                }
                and type(v) is int
                and v >= 0
            }
            if isinstance(usage, dict)
            else {}
        )
        return result
    except Exception as exc:
        detail = {
            **detail,
            "state": "failed",
            "error_code": exc.code if isinstance(exc, ServiceError) else "internal_error",
        }
        raise
    finally:
        detail["duration_ms"] = round((time.monotonic() - started) * 1000)
        with session() as s, s.begin():
            s.get(Audit, record_id).detail = detail
