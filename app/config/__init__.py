from __future__ import annotations

import importlib
from typing import Any

# Motivation vs Logic: the `app.config` package is the public surface for
# settings, shared errors, the DI graph, and FastAPI wiring. `Settings` and
# exception types stay eager so `from app.config import Settings` never pulls in
# the heavy `container` graph; `AppContainer` / `build_container` / `get_container`
# load lazily to break the `config -> agent -> config` import cycle.

from app.config.errors import (
    InventoryError,
    InventoryNotFoundError,
    ParameterMappingError,
    UnsupportedToolError,
    UpstreamServiceError,
)
from app.config.settings import Settings, get_settings

__all__ = [
    "AppContainer",
    "InventoryError",
    "InventoryNotFoundError",
    "ParameterMappingError",
    "Settings",
    "UnsupportedToolError",
    "UpstreamServiceError",
    "build_container",
    "get_container",
    "get_settings",
]


def __getattr__(name: str) -> Any:
    if name in ("AppContainer", "build_container"):
        container = importlib.import_module("app.config.container")
        return getattr(container, name)
    if name == "get_container":
        dependencies = importlib.import_module("app.config.dependencies")
        return dependencies.get_container
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
