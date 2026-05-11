from __future__ import annotations


class AppError(Exception):
    """Base exception for tool orchestration failures."""


class ParameterMappingError(AppError):
    """Raised when a request cannot be safely mapped to tool arguments."""


class UnsupportedToolError(AppError):
    """Raised when a requested tool is not registered."""


class UpstreamServiceError(AppError):
    """Raised when an upstream service returns an operational failure."""

    def __init__(self, status_code: int, detail: str, request: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.request = request
