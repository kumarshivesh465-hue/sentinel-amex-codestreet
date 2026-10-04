"""Cross-cutting security utilities.

Standard library only. This module holds four concerns that every request
touches and that are easy to get subtly wrong:

* **Security headers** - the response headers browsers use to sandbox the app
  (CSP, HSTS, X-Frame-Options, ...). Applied centrally so no endpoint forgets.
* **Rate limiting** - a sliding-window limiter that protects the login and
  signup paths from brute force and the API from abuse.
* **Request IDs** - a short unique id per request, echoed in ``X-Request-ID``
  and attached to every log line for that request.
* **Redaction** - strips passwords, tokens and personal data before anything is
  written to a log.

Note on scale: the rate limiter keeps counters in process memory. That is
correct for this single-process standard-library server; a multi-instance
deployment must move the counters to a shared store (the playbook's point about
in-memory limits). See /docs/monitoring.md and /docs/scaling notes.
"""

from __future__ import annotations

import re
import secrets
import threading
import time

REQUEST_ID_HEADER = "X-Request-ID"


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

def security_headers(is_html: bool = False) -> dict:
    """Headers applied to every response.

    ``is_html`` adds the CSP: the single-page app uses inline styles and inline
    event handlers, so ``'unsafe-inline'`` is currently required for script and
    style. Everything else is locked to ``'self'`` (plus Google Fonts). Removing
    ``'unsafe-inline'`` is a tracked hardening item in the threat model.
    """
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        # Pages: allow the microphone for the voice feature, nothing else.
        "Permissions-Policy": "camera=(), geolocation=(), microphone=(self)",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    }
    if is_html:
        headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
    return headers


# ---------------------------------------------------------------------------
# Request IDs
# ---------------------------------------------------------------------------

def new_request_id() -> str:
    return "REQ-" + secrets.token_hex(8).upper()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|jwt|authorization|auth|api[_-]?key|access[_-]?key|"
    r"cookie|set-cookie|pw[_-]?(hash|salt)|email)",
    re.I,
)
REDACTED = "[redacted]"


def redact(value):
    """Recursively replace values under sensitive keys with ``[redacted]``."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if _SENSITIVE_KEY.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Rate limiting (sliding window, in-process)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter keyed by an arbitrary string."""

    def __init__(self, max_keys: int = 20_000):
        self._hits: dict[str, list] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def check(self, key: str, limit: int, window_seconds: int):
        """Record a hit for ``key``. Returns ``(allowed, retry_after_seconds)``."""
        if limit <= 0:
            return True, 0
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = []
                self._hits[key] = bucket
            while bucket and bucket[0] < cutoff:
                bucket.pop(0)
            if len(bucket) >= limit:
                retry_after = int(bucket[0] + window_seconds - now) + 1
                return False, max(1, retry_after)
            bucket.append(now)
            if len(self._hits) > self._max_keys:
                self._prune(cutoff)
            return True, 0

    def _prune(self, cutoff: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Module-level limiter shared by the server.
limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------

def client_ip(handler) -> str:
    """Best-effort client IP for rate limiting.

    ``X-Forwarded-For`` is intentionally ignored by default: trusting a
    client-supplied header would let an attacker rotate the header to bypass
    rate limits. Deployments behind a known proxy should enable
    ``TRUST_PROXY_HEADERS`` and configure the proxy to overwrite the header.
    """
    try:
        if getattr(handler.server, "trust_proxy_headers", False):
            forwarded = handler.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return handler.client_address[0]
    except Exception:
        return "unknown"
