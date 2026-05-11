from __future__ import annotations

import importlib
from typing import Any

from app.config.errors import (
    AppError,
    ParameterMappingError,
    UnsupportedToolError,
    UpstreamServiceError,
)
from app.config.settings import Settings, get_settings

__all__ = [
    "AppContainer",
    "AppError",
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
