"""Environment-driven configuration, with safe defaults.

Nothing here requires a key or network access. The LLM intent backend stays
disabled until ``LLM_ENABLED=true`` and a provider key are both present, and if
anything goes wrong the server silently falls back to the deterministic
rule-based classifier. That fallback is the source of truth for correctness:
the LLM is a precision upgrade, never a hard dependency.
"""

from __future__ import annotations

import os


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.host = _str("HOST", "0.0.0.0")
        self.port = _int("PORT", 8000)

        # Demo mode exposes the tamper-simulation endpoint. Turn it off for any
        # deployment that is not an explicit demo: it lets a caller corrupt the
        # audit chain on purpose.
        self.demo_mode = _bool("DEMO_MODE", True)

        self.reset_db_on_start = _bool("RESET_DB_ON_START", False)
        self.max_message_chars = _int("MAX_MESSAGE_CHARS", 4000)

        # Real authentication. On by default: every data endpoint then requires
        # a bearer token from /api/auth/login, and sessions are scoped to their
        # owner. Set AUTH_ENABLED=false to restore the open, auth-free demo.
        self.auth_enabled = _bool("AUTH_ENABLED", True)
        self.token_ttl_seconds = _int("TOKEN_TTL_SECONDS", 12 * 3600)
        # When empty, a random per-process secret is generated at import time.
        # That invalidates tokens on restart, which is the safe default; set
        # AUTH_SECRET for durable sessions across restarts.
        self.auth_secret = _str("AUTH_SECRET", "") or os.urandom(32).hex()
        self.auth_secret_configured = bool(os.environ.get("AUTH_SECRET", "").strip())

        # Optional LLM intent backend.
        self.llm_enabled = _bool("LLM_ENABLED", False)
        self.llm_provider = _str("LLM_PROVIDER", "gemini").lower()
        self.llm_model = _str("LLM_MODEL", "gemini-2.0-flash")
        self.llm_api_key = _str(
            "LLM_API_KEY", _str("GEMINI_API_KEY", _str("GOOGLE_API_KEY", ""))
        )
        self.llm_timeout_seconds = _int("LLM_TIMEOUT_SECONDS", 8)

        # Logging: JSON to stdout. "debug" must not be used in production.
        self.log_level = _str("LOG_LEVEL", "INFO").upper()

        # Only trust X-Forwarded-For when we actually sit behind a proxy that
        # overwrites it; otherwise a client could spoof its IP to evade limits.
        self.trust_proxy_headers = _bool("TRUST_PROXY_HEADERS", False)

        # Rate limits (per IP and, for auth, per email). Generous defaults.
        self.rate_limit_auth = _int("RATE_LIMIT_AUTH", 10)       # login/register
        self.rate_limit_auth_window = _int("RATE_LIMIT_AUTH_WINDOW", 900)
        self.rate_limit_api = _int("RATE_LIMIT_API", 300)        # general API
        self.rate_limit_api_window = _int("RATE_LIMIT_API_WINDOW", 60)

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_enabled and self.llm_api_key)

    def validate(self) -> list:
        """Return a list of fatal configuration problems (empty means OK).

        Called at startup so a misconfigured deployment fails loudly instead of
        running in an unsafe state (playbook layer 9).
        """
        problems = []
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            problems.append(f"LOG_LEVEL={self.log_level!r} is not a valid level")
        if not 1 <= self.port <= 65535:
            problems.append(f"PORT={self.port} is out of range")
        if self.max_message_chars < 1:
            problems.append("MAX_MESSAGE_CHARS must be positive")
        if self.token_ttl_seconds < 60:
            problems.append("TOKEN_TTL_SECONDS must be at least 60")
        if not self.demo_mode and not self.auth_enabled:
            problems.append(
                "AUTH_ENABLED=false with DEMO_MODE=false would expose an unauthenticated "
                "API; enable auth or run with DEMO_MODE=true"
            )
        if not self.demo_mode and not self.auth_secret_configured:
            problems.append(
                "AUTH_SECRET must be set outside demo mode, otherwise tokens are lost "
                "on every restart"
            )
        return problems

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "demo_mode": self.demo_mode,
            "max_message_chars": self.max_message_chars,
            "auth_enabled": self.auth_enabled,
            "llm_enabled": self.llm_ready,
            "llm_provider": self.llm_provider if self.llm_ready else None,
        }


settings = Settings()
