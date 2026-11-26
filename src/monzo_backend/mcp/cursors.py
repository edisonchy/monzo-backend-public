"""Signed, revision-bound keyset cursors. No server-side session or open snapshot."""

import base64
import binascii
import hashlib
import hmac
import json
import time


class ToolFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Cursors:
    def __init__(self, secret, owner):
        self.secret = secret.encode()
        self.owner = owner

    def log_fingerprint(self, tool, values):
        return hmac.new(
            self.secret, canonical([self.owner, tool, values]).encode(), hashlib.sha256
        ).hexdigest()

    def fingerprint(self, tool, inputs):
        values = inputs.model_dump(mode="json", exclude={"cursor", "expected_revision", "limit"})
        if tool == "search_transactions" and values.get("mode") == "payments":
            del values["mode"]
        if tool == "search_transactions" and values.get("reported_spending_only") is False:
            del values["reported_spending_only"]
        return hmac.new(
            self.secret, canonical([self.owner, tool, values]).encode(), hashlib.sha256
        ).hexdigest()

    def encode(self, fingerprint, revision, position):
        raw = canonical(
            {"f": fingerprint, "r": revision, "p": position, "e": int(time.time()) + 900}
        ).encode()
        tag = hmac.new(self.secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + tag).decode()

    def decode(self, value, fingerprint, revision):
        if not value:
            return None
        try:
            packed = base64.b64decode(value, altchars=b"-_", validate=True)
            raw, tag = packed[:-32], packed[-32:]
            if not hmac.compare_digest(tag, hmac.new(self.secret, raw, hashlib.sha256).digest()):
                raise ValueError()
            obj = json.loads(raw)
            if obj["f"] != fingerprint or obj["e"] <= time.time():
                raise ValueError()
            if obj["r"] != revision:
                raise ToolFailure(
                    "data_changed", "Data changed. Restart the query without a cursor."
                )
            return obj["p"]
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
            raise ToolFailure(
                "validation_error", "Invalid or expired cursor. Restart the query."
            ) from None
