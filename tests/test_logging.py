from __future__ import annotations

import logging

from app.config.logging import LargePayloadLogFilter, configure_logging


def test_large_payload_filter_normalizes_base64_like_log_messages() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="%s",
        args=("A" * 2048,),
        exc_info=None,
    )

    assert LargePayloadLogFilter().filter(record) is True

    rendered = record.getMessage()
    assert "omitted base64-like log payload" in rendered
    assert "chars=2048" in rendered
    assert "A" * 512 not in rendered


def test_configure_logging_keeps_mcp_protocol_bodies_above_debug() -> None:
    configure_logging("debug")

    assert logging.getLogger("mcp.server.lowlevel.server").getEffectiveLevel() == logging.INFO
