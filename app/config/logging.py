from __future__ import annotations

import hashlib
import logging
import re
from logging.config import dictConfig
from typing import Any


_BASE64_LIKE_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_BASE64_LOG_MIN_CHARS = 512
_LOG_VALUE_MAX_CHARS = 4000
_LOG_VALUE_HEAD_CHARS = 2000


def _summarize_large_log_value(value: Any) -> Any:
    if isinstance(value, str):
        compact = "".join(value.split())
        if len(compact) >= _BASE64_LOG_MIN_CHARS and _BASE64_LIKE_RE.fullmatch(value):
            digest = hashlib.sha256(compact.encode("utf-8", errors="ignore")).hexdigest()[:12]
            return (
                "[omitted base64-like log payload "
                f"chars={len(value)} sha256={digest} prefix={compact[:24]}]"
            )
        if len(value) > _LOG_VALUE_MAX_CHARS:
            digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]
            return f"{value[:_LOG_VALUE_HEAD_CHARS]}...[truncated log payload chars={len(value)} sha256={digest}]"
        return value

    if isinstance(value, dict):
        return {key: _summarize_large_log_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_summarize_large_log_value(item) for item in value)
    if isinstance(value, list):
        return [_summarize_large_log_value(item) for item in value]
    return value


class LargePayloadLogFilter(logging.Filter):
    # Root Cause vs Logic: MCP protocol DEBUG logs can contain native image
    # content as base64, which turns one useful request log into megabytes of
    # opaque text. Normalize large/base64-like fields at the handler boundary so
    # app logs keep request shape without dumping binary payloads.
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _summarize_large_log_value(record.msg)
        if record.args:
            record.args = _summarize_large_log_value(record.args)
        return True


def configure_logging(level: str) -> None:
    normalized_level = level.upper()
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                }
            },
            "filters": {
                "large_payloads": {
                    "()": LargePayloadLogFilter,
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["large_payloads"],
                }
            },
            "root": {
                "level": normalized_level,
                "handlers": ["console"],
            },
        }
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Motivation vs Logic: httpcore is embedded inside httpx and defaults to DEBUG,
    # which flooded the console with every handshake/request detail during normal use.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpcore.connection").setLevel(logging.WARNING)
    logging.getLogger("httpcore.http11").setLevel(logging.WARNING)
    # Motivation vs Logic: Uvicorn's access/error loggers repeat requests and overshadow our API-level context.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)
    # Motivation vs Logic: application DEBUG remains useful, but the MCP SDK's
    # low-level server DEBUG path logs complete protocol messages, including
    # large native image payloads. Keep SDK request lifecycle logs without
    # emitting full JSON-RPC bodies.
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.INFO)
