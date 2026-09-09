"""Small, dependency-free security helpers shared by every Flask service in
this project (app_main.py, app_helper.py, relay_server.py): a per-IP sliding
window rate limiter and a standard set of hardening response headers.

Kept dependency-free on purpose, matching the rest of this project's
zero-extra-deps philosophy (see relay_server.py's own docstring) — pulling in
Flask-Limiter/Flask-Talisman for a LAN demo tool would be a heavier
dependency footprint than the protection is worth here.
"""
import functools
import hmac
import threading
import time
from collections import defaultdict, deque

from flask import jsonify, request


class RateLimiter:
    """Sliding-window rate limiter, keyed by caller IP. Good enough to blunt
    casual abuse/accidental hammering on a LAN tool; not a substitute for a
    real reverse-proxy rate limiter if a service here (relay_server.py) is
    ever exposed to the open internet at scale."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window_seconds:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True

    def prune(self, max_age_seconds: float = 3600):
        """Drop tracked keys with no recent activity, so a long-running
        deployment doesn't accumulate one deque per distinct IP forever."""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, q in self._hits.items() if not q or now - q[-1] > max_age_seconds]
            for k in stale:
                self._hits.pop(k, None)


def _client_key() -> str:
    # X-Forwarded-For is attacker-controllable when this app is reached
    # directly (no trusted proxy in front), so it's deliberately not
    # consulted here — request.remote_addr reflects the actual TCP peer.
    return request.remote_addr or "unknown"


def rate_limited(limiter: RateLimiter):
    """Decorator: reject with 429 once `limiter`'s window is exceeded for
    the caller's IP. Applied to every state-changing/expensive endpoint."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if not limiter.allow(_client_key()):
                resp = jsonify({"error": "too many requests — slow down and try again"})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(int(limiter.window_seconds))
                return resp
            return fn(*args, **kwargs)

        return wrapped

    return decorator


def apply_security_headers(resp, csp: str = None):
    """A conservative baseline: no framing, no MIME sniffing, no referrer
    leakage, no unneeded browser feature access. Safe defaults for a
    same-origin dashboard/API with no third-party embeds."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), payment=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if csp:
        resp.headers.setdefault("Content-Security-Policy", csp)
    return resp


def constant_time_eq(a: str, b: str) -> bool:
    """hmac.compare_digest wrapper so every passphrase/token check in the
    codebase goes through one obviously-correct, timing-safe helper."""
    return hmac.compare_digest(a or "", b or "")
