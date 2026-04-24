from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.config import get_container
from app.render import render_mock


router = APIRouter()


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
    }


@router.get("/system/spec")
async def system_spec(container = Depends(get_container)) -> dict[str, object]:
    return {
        "server_name": container.settings.server_name,
        "server_version": container.settings.server_version,
        "integration_surfaces": {
            "rest": {
                "transport": "http",
                "base_path": container.settings.api_prefix,
                "notes": "Custom diagnostic and query endpoints for local testing, the mock UI, and the stock-first debug payload.",
            },
            "mcp": {
                "transport": "stdio",
                "entrypoint": "python3 -m app.mcp.server",
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
    }


def _build_ui_response(container) -> HTMLResponse:
    if not container.settings.enable_mock_ui_simulation:
        return HTMLResponse("<p>UI simulation is disabled.</p>", status_code=404)

    path = container.settings.resolve_path(container.settings.mock_ui_path)
    if not path.exists():
        return HTMLResponse("<p>UI template is missing.</p>", status_code=404)

    return HTMLResponse(render_mock(path=path, settings=container.settings))


@router.get("/ui", response_class=HTMLResponse)
async def ui(container = Depends(get_container)) -> HTMLResponse:
    return _build_ui_response(container)


@router.get("/mock-ui", response_class=HTMLResponse)
async def mock_ui_legacy(container = Depends(get_container)) -> HTMLResponse:
    return _build_ui_response(container)
