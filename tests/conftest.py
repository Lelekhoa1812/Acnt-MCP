"""Pytest hooks and shared fixtures for the test suite."""

from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    # Motivation vs Logic: trio runs duplicate async tests against Redis; the
    # suite uses asyncio-only patterns and invalid-port Redis fallback, so pin
    # AnyIO to asyncio for predictable local runs.
    return "asyncio"
