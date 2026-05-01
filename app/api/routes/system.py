from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.auth import IdentityAuthError
from app.auth.claude import ClaudeOAuthError
from app.config import get_container
from app.render import render_mock, render_static_html, render_usage_stats_html


router = APIRouter()


class OAuthGroupSettingsPayload(BaseModel):
    oauth_user_group: str = Field(alias="OAUTH_USER_GROUP")
    news_pl_group: str = Field(alias="NEWS_PL_GROUP")
    weather_pl_group: str = Field(alias="WEATHER_PL_GROUP")
    currency_pl_group: str = Field(alias="CURRENCY_PL_GROUP")
    stock_pl_group: str = Field(alias="STOCK_PL_GROUP")


_GROUP_ENV_KEYS = (
    "OAUTH_USER_GROUP",
    "NEWS_PL_GROUP",
    "WEATHER_PL_GROUP",
    "CURRENCY_PL_GROUP",
    "STOCK_PL_GROUP",
)


def _extract_bearer_token(request: Request) -> str | None:
    raw = request.headers.get("authorization")
    if not raw:
        return None
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None


def _oauth_admin_config(container) -> dict[str, str | None]:
    settings = container.settings
    return {
        "clientId": settings.oauth_client_id,
        "tenantId": settings.oauth_tenant_id,
        "authority": settings.resolved_oauth_authority,
        "scope": " ".join(["openid", "profile", "email", "offline_access", *settings.parsed_oauth_graph_scopes]),
        "clientAuthMethod": settings.resolved_oauth_client_auth_method,
    }


def _oauth_group_config(container) -> dict[str, str]:
    settings = container.settings
    return {
        "OAUTH_USER_GROUP": settings.oauth_user_group,
        "NEWS_PL_GROUP": settings.news_pl_group,
        "WEATHER_PL_GROUP": settings.weather_pl_group,
        "CURRENCY_PL_GROUP": settings.currency_pl_group,
        "STOCK_PL_GROUP": settings.stock_pl_group,
    }


def _require_oauth_admin_user(request: Request, container) -> None:
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail={"code": "missing_bearer_token", "message": "Bearer token required."})
    try:
        claims = request.app.state.claude_oauth_service.validate_access_token(token)
        request.app.state.identity_gateway.authorize_claims(claims)
    except ClaudeOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    except IdentityAuthError as exc:
        raise HTTPException(status_code=exc.payload.status_code, detail=exc.to_response_payload()["error"]) from exc


def _quote_env_value(value: str) -> str:
    if not value:
        return '""'
    if any(char.isspace() for char in value) or any(char in value for char in '#"\''):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines.append(f"{key}={_quote_env_value(updates[key])}")
            seen.add(key)
        else:
            lines.append(line)
    for key in _GROUP_ENV_KEYS:
        if key not in seen:
            lines.append(f"{key}={_quote_env_value(updates[key])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _apply_group_settings(container, updates: dict[str, str]) -> None:
    settings = container.settings
    # Motivation vs Logic: the admin page is an operational control surface, so
    # saving should update durable `.env` and the live Settings instance together
    # instead of requiring an avoidable service restart for permission changes.
    settings.oauth_user_group = updates["OAUTH_USER_GROUP"]
    settings.news_pl_group = updates["NEWS_PL_GROUP"]
    settings.weather_pl_group = updates["WEATHER_PL_GROUP"]
    settings.currency_pl_group = updates["CURRENCY_PL_GROUP"]
    settings.stock_pl_group = updates["STOCK_PL_GROUP"]


@router.get("/health")
async def health(container = Depends(get_container)) -> dict[str, object]:
    kvs = container.key_value_store
    return {
        "status": "ok",
        "service": container.settings.server_name,
        "version": container.settings.server_version,
        "data_source": container.settings.data_source_label,
        "session_cache_backend": kvs.persistence_backend,
        "redis_client_connected": kvs.redis_client_connected,
        "redis_fallback_enabled": container.settings.redis_fallback_enabled,
        "local_chat_memory_enabled": container.settings.local_chat_memory_enabled,
        "local_chat_memory_turns": container.settings.local_chat_memory_turns,
        "harmonise_inventory_tools_enabled": container.harmonise_inventory_tools_enabled,
    }


@router.get("/system/spec")
async def system_spec(container = Depends(get_container)) -> dict[str, object]:
    return {
        "server_name": container.settings.server_name,
        "server_version": container.settings.server_version,
        "logo_url": container.settings.resolved_server_logo_url,
        "integration_surfaces": {
            "rest": {
                "transport": "http",
                "base_path": container.settings.api_prefix,
                "notes": "Custom diagnostic and query endpoints for local testing, the mock UI, and the stock-first debug payload.",
            },
            "mcp": {
                "transport": "stdio",
                "entrypoint": "python3 -m app.mcp.server",
                "logo_url": container.settings.resolved_server_logo_url,
                "notes": "Protocol-compliant MCP tool server for Claude/Cursor-style integrations.",
            },
        },
        "phase": "phase1",
        "persona": "Harmonise Orchestrator",
        "scope": [
            "inventory lookup",
            "product and variant resolution",
            "product specifications",
            "stock visibility",
            "product comparison",
            "clarification of ambiguous product requests",
            "grounded Q&A and lightweight planning based on inventory evidence",
            "external plugin exploration for weather, news, and currency",
            "separate audit/debug payloads for planner and retrieval traces",
        ],
        "out_of_scope": [
            "booking workflows not yet implemented in the current tool contract",
            "quote workflows not yet implemented in the current tool contract",
            "reservation workflows not yet implemented in the current tool contract",
            "event line item workflows not yet implemented in the current tool contract",
        ],
        "startup_notes": container.settings.startup_notes(),
        "harmonise_inventory_tools_enabled": container.harmonise_inventory_tools_enabled,
    }


def _build_ui_response(container) -> HTMLResponse:
    if not container.settings.enable_mock_ui_simulation:
        return HTMLResponse("<p>UI simulation is disabled.</p>", status_code=404)

    path = container.settings.resolve_path(container.settings.mock_ui_path)
    if not path.exists():
        return HTMLResponse("<p>UI template is missing.</p>", status_code=404)

    return HTMLResponse(render_mock(path=path, settings=container.settings))


def _build_oauth_response(container) -> HTMLResponse:
    oauth_path = container.settings.resolve_path("./ui/oauth/index.html")
    if not oauth_path.exists():
        return HTMLResponse("<p>OAuth helper template is missing.</p>", status_code=404)

    # Motivation vs Logic: the OAuth helper is a lightweight operator page,
    # so we serve the authored HTML directly from the API namespace that the
    # web deployment already exposes instead of adding another build step.
    return HTMLResponse(render_static_html(oauth_path))


@router.get("/chat", response_class=HTMLResponse)
async def chat_ui(container = Depends(get_container)) -> HTMLResponse:
    return _build_ui_response(container)


@router.get("/ui", response_class=HTMLResponse)
async def ui(container = Depends(get_container)) -> HTMLResponse:
    # Legacy alias: same shell as /chat; prefer /chat for new links.
    return _build_ui_response(container)


@router.get("/mock-ui", response_class=HTMLResponse)
async def mock_ui_legacy(container = Depends(get_container)) -> HTMLResponse:
    # Legacy alias: same shell as /chat; prefer /chat for new links.
    return _build_ui_response(container)


@router.get("/oauth", response_class=HTMLResponse)
async def oauth_ui(container = Depends(get_container)) -> HTMLResponse:
    return _build_oauth_response(container)


@router.get("/oauth/config")
async def oauth_config(request: Request, container = Depends(get_container)) -> dict[str, Any]:
    payload: dict[str, Any] = {"msal": _oauth_admin_config(container), "authenticated": False}
    if not _extract_bearer_token(request):
        return payload
    _require_oauth_admin_user(request, container)
    payload["authenticated"] = True
    payload["groups"] = _oauth_group_config(container)
    return payload


@router.put("/oauth/groups")
async def oauth_groups(
    payload: OAuthGroupSettingsPayload,
    request: Request,
    container = Depends(get_container),
) -> dict[str, Any]:
    _require_oauth_admin_user(request, container)
    updates = payload.model_dump(by_alias=True)
    env_path = container.settings.resolve_path(".env")
    _update_env_file(env_path, updates)
    _apply_group_settings(container, updates)
    request.app.state.identity_gateway.clear_group_caches()
    return {"status": "ok", "groups": _oauth_group_config(container)}


@router.get("/stats", response_class=HTMLResponse)
async def stats(container = Depends(get_container)) -> HTMLResponse:
    snapshot = await container.usage_stats_service.snapshot()
    return HTMLResponse(render_usage_stats_html(snapshot))
