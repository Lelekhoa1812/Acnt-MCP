from __future__ import annotations

from html import escape
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth.claude import ClaudeOAuthError, ClaudeOAuthService
from app.config import get_container


router = APIRouter()


def _wants_explicit_json(request: Request) -> bool:
    format_hint = request.query_params.get("format", "").strip().lower()
    if format_hint == "json":
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept.lower()


def _oauth_callback_page(*, success: bool, title: str, detail: str) -> HTMLResponse:
    accent = "#34d399" if success else "#fb7185"
    heading = "OAuth login complete" if success else "OAuth login failed"
    # Root Cause vs Logic: GPT/Cursor/Claude browser connectors should never be
    # stranded on a raw token dump. The callback now renders a small completion
    # page so accidental browser hits are safe, while explicit JSON opt-ins can
    # still retrieve non-secret diagnostics for debugging.
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escape(title)}</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #07111d;
        --panel: rgba(11, 20, 34, 0.92);
        --border: rgba(148, 163, 184, 0.16);
        --text: #e7eef9;
        --muted: #9aa8bf;
        --accent: {accent};
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 24px;
        background:
          radial-gradient(circle at top left, rgba(125, 211, 252, 0.14), transparent 26%),
          linear-gradient(160deg, #07111d, #0d1727);
        color: var(--text);
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        width: min(560px, 100%);
        border: 1px solid var(--border);
        border-radius: 24px;
        background: var(--panel);
        padding: 28px;
        box-shadow: 0 30px 90px rgba(0, 0, 0, 0.4);
      }}
      .eyebrow {{
        margin: 0 0 14px;
        font-size: 0.76rem;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--accent);
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: clamp(1.8rem, 4vw, 2.4rem);
        line-height: 1;
      }}
      p {{
        margin: 0;
        color: var(--muted);
        line-height: 1.55;
      }}
    </style>
  </head>
  <body>
    <main>
      <p class="eyebrow">HTH MCP OAuth</p>
      <h1>{escape(heading)}</h1>
      <p>{escape(detail)}</p>
    </main>
  </body>
</html>"""
    return HTMLResponse(content=html)


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


def _consume_state(request: Request, state: str | None) -> dict[str, Any] | None:
    if not state:
        return None
    record: dict[str, Any] | None = request.app.state.oauth_login_states.pop(state, None)
    if not record:
        return None
    if record.get("expires_at", 0.0) < time.time():
        return None
    return record


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
    # default state mirrors the standard Claude MCP connector example
    state = request.query_params.get("state") or "claude"
    authorize_url, code_verifier = service.build_authorize_url(base_url=base_url, state=state)
    request.app.state.oauth_login_states[state] = {
        "expires_at": time.time() + 600,
        "code_verifier": code_verifier,
    }
    return RedirectResponse(authorize_url, status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, container=Depends(get_container)) -> Response:
    service = _get_oauth_service(request)
    base_url = _base_url_for_request(request)
    params = request.query_params

    if params.get("error"):
        error_payload = {
            "error": params.get("error"),
            "error_description": params.get("error_description"),
        }
        if _wants_explicit_json(request):
            return JSONResponse(status_code=400, content=error_payload)
        return _oauth_callback_page(
            success=False,
            title="OAuth login failed",
            detail=str(params.get("error_description") or params.get("error") or "Authentication failed."),
        )

    code = params.get("code")
    state = params.get("state")
    if not code:
        payload = {"error": "invalid_request", "error_description": "Missing code."}
        if _wants_explicit_json(request):
            return JSONResponse(status_code=400, content=payload)
        return _oauth_callback_page(success=False, title="OAuth login failed", detail=payload["error_description"])
    state_record = _consume_state(request, state)
    if not state_record:
        payload = {"error": "invalid_state", "error_description": "State mismatch."}
        if _wants_explicit_json(request):
            return JSONResponse(status_code=400, content=payload)
        return _oauth_callback_page(success=False, title="OAuth login failed", detail=payload["error_description"])

    try:
        token_payload = await service.exchange_code_for_token(
            base_url=base_url,
            code=code,
            code_verifier=str(state_record.get("code_verifier") or ""),
        )
        claims = service.validate_access_token(str(token_payload["access_token"]))
    except ClaudeOAuthError as exc:
        if _wants_explicit_json(request):
            return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "error_description": exc.message})
        return _oauth_callback_page(success=False, title="OAuth login failed", detail=exc.message)

    if _wants_explicit_json(request):
        return JSONResponse(
            content={
                "status": "ok",
                "token_type": token_payload.get("token_type", "Bearer"),
                "expires_in": token_payload.get("expires_in"),
                "scope": token_payload.get("scope"),
                "claims": claims,
            }
        )

    return _oauth_callback_page(
        success=True,
        title="OAuth login complete",
        detail=(
            "Authentication finished successfully. You can close this window and return to the client that started the login."
        ),
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
