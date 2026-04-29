from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.claude import ClaudeOAuthError, ClaudeOAuthService
from app.config import get_container


router = APIRouter()


def _base_url_for_request(request: Request) -> str:
    settings = request.app.state.container.settings
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _get_oauth_service(request: Request) -> ClaudeOAuthService:
    service = getattr(request.app.state, "claude_oauth_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="Claude OAuth service is not initialized.")
    return service


def _remember_state(request: Request, state: str) -> None:
    request.app.state.oauth_login_states[state] = {"expires_at": time.time() + 600}


def _consume_state(request: Request, state: str | None) -> bool:
    if not state:
        return False
    record: dict[str, Any] | None = request.app.state.oauth_login_states.pop(state, None)
    if not record:
        return False
    if record.get("expires_at", 0.0) < time.time():
        return False
    return True


def _extract_access_token(request: Request) -> str | None:
    token = ClaudeOAuthService.extract_bearer_token({k.lower(): v for k, v in request.headers.items()})
    if token:
        return token
    params = request.query_params
    if params.get("access_token"):
        return params["access_token"]
    return None


@router.get("/oauth/login")
async def oauth_login(request: Request, container=Depends(get_container)) -> RedirectResponse:
    service = _get_oauth_service(request)
    base_url = _base_url_for_request(request)
    state = request.query_params.get("state") or secrets.token_urlsafe(32)
    _remember_state(request, state)
    return RedirectResponse(service.build_authorize_url(base_url=base_url, state=state), status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, container=Depends(get_container)) -> JSONResponse:
    service = _get_oauth_service(request)
    base_url = _base_url_for_request(request)
    params = request.query_params

    if params.get("error"):
        return JSONResponse(
            status_code=400,
            content={
                "error": params.get("error"),
                "error_description": params.get("error_description"),
            },
        )

    code = params.get("code")
    state = params.get("state")
    if not code:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "Missing code."})
    if not _consume_state(request, state):
        return JSONResponse(status_code=400, content={"error": "invalid_state", "error_description": "State mismatch."})

    try:
        token_payload = await service.exchange_code_for_token(base_url=base_url, code=code)
        claims = service.validate_access_token(str(token_payload["access_token"]))
    except ClaudeOAuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "error_description": exc.message})

    return JSONResponse(
        content={
            "status": "ok",
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_in": token_payload.get("expires_in"),
            "scope": token_payload.get("scope"),
            "access_token": token_payload["access_token"],
            "id_token": token_payload.get("id_token"),
            "refresh_token": token_payload.get("refresh_token"),
            "claims": claims,
        }
    )


@router.post("/oauth/token/validate")
@router.get("/oauth/token/validate")
async def oauth_token_validate(request: Request, container=Depends(get_container)) -> JSONResponse:
    service = _get_oauth_service(request)
    token = _extract_access_token(request)
    if not token:
        return JSONResponse(status_code=401, content={"error": "missing_token", "error_description": "Bearer token required."})

    try:
        claims = service.validate_access_token(token)
    except ClaudeOAuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "error_description": exc.message})

    return JSONResponse(content={"active": True, "claims": claims})
