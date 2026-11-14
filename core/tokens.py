"""Tiny signed tokens (HMAC-SHA256) for waiting-room tickets and access passes.

Stateless: any API pod can verify any token without a database read,
which matters when a million people poll "is it my turn yet?".
"""
import base64
import hashlib
import hmac
import json
import time


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret, payload):
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def verify(secret, token, kind=None, now=None):
    """Return the payload if the signature is valid and not expired, else None."""
    try:
        body, mac = token.split(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(mac)):
            return None
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if kind and payload.get("k") != kind:
        return None
    if "exp" in payload and payload["exp"] < (now or time.time()):
        return None
    return payload
