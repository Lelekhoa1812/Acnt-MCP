from __future__ import annotations

from fastapi import HTTPException, Request

from app.auth import IdentityAuthError, UserContext
from app.config.settings import Settings


def resolve_user_context_or_none(request: Request, settings: Settings) -> UserContext | None:
    gateway = request.app.state.identity_gateway
    if not gateway.enabled:
        return None

    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        return gateway.authenticate_headers(headers)
    except IdentityAuthError as exc:
        raise HTTPException(status_code=exc.payload.status_code, detail=exc.to_response_payload()["error"]) from exc


def resolve_optional_user_context(request: Request, settings: Settings) -> UserContext | None:
    # Motivation vs Logic: the public chat UI is intentionally open, while MCP
    # and REST tool routes remain protected in identity mode. If a chat caller
    # sends a bearer token we validate and use it; otherwise chat proceeds as an
    # anonymous local REST session.
    if not request.headers.get("authorization"):
        return None
    return resolve_user_context_or_none(request, settings)
