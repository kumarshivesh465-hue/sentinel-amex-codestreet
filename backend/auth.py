"""Authentication primitives: password hashing and signed bearer tokens.

Standard library only, by project design.

Two pieces live here:

* **Password storage** - PBKDF2-HMAC-SHA256 with a per-user random salt. A
  plain hash would let anyone with the database crack weak passwords with a
  rainbow table; PBKDF2 makes each guess expensive. The iteration count is
  deliberately high but still fast enough for a login request.

* **Session tokens** - stateless, HMAC-SHA256 signed. The token carries the
  user id and an expiry, and the signature proves the server issued it. This
  means no server-side session table is needed and a restart does not lose
  sessions *as long as the signing secret is stable*. When no secret is
  configured one is generated per process, so tokens simply do not survive a
  restart - a safe default for a demo, and ``AUTH_SECRET`` makes them durable.

Nothing here trusts caller input: signatures are compared with
``hmac.compare_digest`` (constant time), tokens are length-checked before
parsing, and every failure returns ``None`` rather than raising.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_ROLES = {"cardmember", "supervisor"}


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str, salt_hex: str = None):
    """Return ``(salt_hex, hash_hex)`` for a password.

    Pass an existing ``salt_hex`` to re-hash (used by verification); otherwise a
    fresh random salt is generated.
    """
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, expected_hash: str) -> bool:
    if not password or not salt_hex or not expected_hash:
        return False
    try:
        _, computed = hash_password(password, salt_hex)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, expected_hash)


def password_problem(password: str):
    """Return a human-readable reason the password is unacceptable, or ``None``."""
    if not password or len(password) < 8:
        return "password must be at least 8 characters"
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "password must contain both letters and a number"
    return None


def email_problem(email: str):
    if not email or not _EMAIL_RE.match(email):
        return "a valid email address is required"
    return None


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(user: dict, secret: str, ttl_seconds: int) -> str:
    """Create a signed token for ``user`` that expires in ``ttl_seconds``."""
    payload = {
        "sub": user["id"],
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "role": user.get("role", "cardmember"),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(signature)}"


def verify_token(token: str, secret: str):
    """Return the token payload, or ``None`` if invalid/expired/tampered."""
    if not token or not isinstance(token, str) or token.count(".") != 1:
        return None
    body, signature = token.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64d(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("sub") or int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload
