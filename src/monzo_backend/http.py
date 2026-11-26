import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from monzo_backend.config import settings


class ServiceError(Exception):
    def __init__(self, code, retryable=True, retry_after=0):
        self.code, self.retryable, self.retry_after = code, retryable, retry_after
        super().__init__(code)  # Never put upstream bodies, URLs or credentials in exceptions.


def retry_delay(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError):
            return 0


def request(method, url, *, service="provider", timeout_seconds=None, **kwargs):
    cfg = settings()
    timeout = cfg.request_timeout if timeout_seconds is None else timeout_seconds
    deadline = time.monotonic() + timeout
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream(method, url, **kwargs) as response:
                if response.status_code >= 300:
                    code = f"{service}_http_{response.status_code}"
                    raise ServiceError(
                        code,
                        response.status_code in (408, 429) or response.status_code >= 500,
                        retry_delay(response.headers.get("retry-after")),
                    )
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000 or time.monotonic() > deadline:
                        raise ServiceError(f"{service}_response_limit")
                    chunks.append(chunk)
                return json.loads(b"".join(chunks))
    except (httpx.HTTPError, OSError):
        raise ServiceError(f"{service}_network_error") from None
    except (ValueError, UnicodeError):
        raise ServiceError(f"{service}_invalid_json") from None
