from __future__ import annotations


class InventoryError(Exception):
    """Base exception for inventory orchestration failures."""


class ParameterMappingError(InventoryError):
    """Raised when a request cannot be safely mapped to tool arguments."""


class UnsupportedToolError(InventoryError):
    """Raised when a requested tool is not registered."""


class InventoryNotFoundError(InventoryError):
    """Raised when an exact lookup returns no record."""


class UpstreamServiceError(InventoryError):
    """Raised when the Harmonise upstream returns an operational failure."""

    def __init__(self, status_code: int, detail: str, request: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.request = request
