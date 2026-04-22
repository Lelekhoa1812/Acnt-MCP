from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.dependencies import get_container
from app.render import render_mock


router = APIRouter()


@router.get("/health")
async def health(container = Depends(get_container)) -> dict[str, str]:
    return {
        "status": "ok",
        "service": container.settings.server_name,
        "version": container.settings.server_version,
        "data_source": container.settings.data_source_label,
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
                "notes": "Custom diagnostic and query endpoints for local testing and the mock UI.",
            },
            "mcp": {
                "transport": "stdio",
                "entrypoint": "python -m app.mcp.server",
                "notes": "Protocol-compliant MCP tool server for Claude/Cursor-style integrations.",
            },
        },
        "phase": "phase1",
        "scope": [
            "inventory lookup",
            "product and variant resolution",
            "product specifications",
            "stock visibility",
            "product comparison",
            "clarification of ambiguous product requests",
            "grounded Q&A and lightweight planning based on inventory evidence",
            "external plugin exploration for weather, news, and currency",
        ],
        "out_of_scope": [
            "booking logic",
            "quote logic",
            "reservation workflows",
            "event line item workflows",
        ],
        "startup_notes": container.settings.startup_notes(),
    }


@router.get("/mock-ui", response_class=HTMLResponse)
async def mock_ui(container = Depends(get_container)) -> HTMLResponse:
    if not container.settings.enable_mock_ui_simulation:
        return HTMLResponse("<p>Mock UI simulation is disabled.</p>", status_code=404)

    path = container.settings.resolve_path(container.settings.mock_ui_path)
    if not path.exists():
        return HTMLResponse("<p>Mock UI template is missing.</p>", status_code=404)

    return HTMLResponse(render_mock(path=path, settings=container.settings))
