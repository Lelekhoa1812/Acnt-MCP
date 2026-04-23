from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.agent import router as agent_router
from app.api.routes.system import router as system_router
from app.api.routes.tools import build_tools_router
from app.config import (
    InventoryNotFoundError,
    ParameterMappingError,
    Settings,
    UnsupportedToolError,
    UpstreamServiceError,
    build_container,
    get_settings,
)
from app.config.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    logger = logging.getLogger("hth")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for note in resolved_settings.startup_notes():
            logger.warning("startup_note=%s", note)
        container = await build_container(resolved_settings)
        app.state.container = container
        yield
        await container.close()

    app = FastAPI(
        title=resolved_settings.server_name,
        version=resolved_settings.server_version,
        lifespan=lifespan,
    )

    api_tools_router = build_tools_router()

    app.include_router(system_router, prefix=resolved_settings.api_prefix, tags=["system"])
    app.include_router(agent_router, prefix=resolved_settings.api_prefix, tags=["agent"])
    # Root Cause vs Logic: the old app mirrored the REST tool routes under `/mcp`,
    # which looked MCP-like but skipped the real JSON-RPC/stdin protocol entirely.
    # The HTTP app now exposes only the custom REST surface, while real MCP lives
    # in the dedicated stdio server entrypoint.
    app.include_router(api_tools_router, prefix=resolved_settings.api_prefix, tags=["tools"])

    if resolved_settings.enable_mock_ui_simulation:
        mock_ui_assets_path = resolved_settings.resolve_path(resolved_settings.mock_ui_path).parent / "assets"
        if mock_ui_assets_path.exists():
            app.mount(
                f"{resolved_settings.api_prefix}/ui/assets",
                StaticFiles(directory=mock_ui_assets_path),
                name="ui-assets",
            )
            # Root Cause vs Logic: callers are migrating from `/mock-ui` to
            # `/ui`; we mount both asset paths so old links do not white-screen.
            app.mount(
                f"{resolved_settings.api_prefix}/mock-ui/assets",
                StaticFiles(directory=mock_ui_assets_path),
                name="mock-ui-assets-legacy",
            )

    @app.exception_handler(ParameterMappingError)
    async def handle_parameter_mapping_error(_: Request, exc: ParameterMappingError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(UnsupportedToolError)
    async def handle_unsupported_tool(_: Request, exc: UnsupportedToolError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InventoryNotFoundError)
    async def handle_inventory_not_found(_: Request, exc: InventoryNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(UpstreamServiceError)
    async def handle_upstream_error(_: Request, exc: UpstreamServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "status_code": exc.status_code},
        )

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "service": resolved_settings.server_name,
            "version": resolved_settings.server_version,
            "transport": resolved_settings.transport,
            "api_prefix": resolved_settings.api_prefix,
            "mcp_transport": "stdio",
            "mcp_entrypoint": "python -m app.mcp.server",
        }

    return app


app = create_app()
