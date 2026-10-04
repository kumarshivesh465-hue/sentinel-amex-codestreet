"""Structured JSON logging.

Every log line is a single JSON object so logs are machine-searchable, and every
request carries a request id that appears in each line it produces and in the
``X-Request-ID`` response header. That is how you trace one user's request
through the system (playbook layer 13).

Sensitive values are redacted before they are written. Redaction happens at the
logger boundary via :func:`backend.security.redact`, so a password or bearer
token can never reach the log file even if a caller passes it in extra fields.
"""

from __future__ import annotations

import json
import logging
import sys

try:
    from . import security
except ImportError:  # executed as a top-level script
    import security

LOGGER_NAME = "sentinel"
_configured = False


class JsonFormatter(logging.Formatter):
    # Context attributes that are copied onto the log record by ``log_event``.
    CONTEXT_FIELDS = (
        "event",
        "request_id",
        "method",
        "path",
        "status",
        "duration_ms",
        "user_id",
        "role",
        "client",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in self.CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(security.redact(payload), default=str)


def configure(level: str = "INFO") -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.handlers = [handler]
        logger.propagate = False
        _configured = True
    # ``debug`` must be off in production; the level is set from configuration.
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(message: str, level: str = "info", **fields) -> None:
    """Log a structured event. Extra fields are attached for the formatter."""
    get_logger().log(
        getattr(logging, str(level).upper(), logging.INFO), message, extra=fields
    )
