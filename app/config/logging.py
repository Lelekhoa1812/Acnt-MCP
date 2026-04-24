from __future__ import annotations

import logging
from logging.config import dictConfig


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
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
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
