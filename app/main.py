from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp.server import build_mcp_server

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


class McpTransportASGI:
    def __init__(self, manager: StreamableHTTPSessionManager, settings: Settings) -> None:
        self.manager = manager
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            response = JSONResponse(
                status_code=405,
                content={"detail": "MCP transports only accept HTTP(S) requests."},
            )
            await response(scope, receive, send)
            return

        if not self._is_authorized(scope):
            response = JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid HTH_MCP_BEARER_TOKEN."},
            )
            await response(scope, receive, send)
            return

        try:
            await self.manager.handle_request(scope, receive, send)
        except RuntimeError as exc:
            if "Task group is not initialized" not in str(exc):
                raise
            # Root Cause vs Logic: bare `/mcp` HTTP requests could hit the mount
            # before the streamable session manager had entered its task group,
            # leaking an internal runtime error instead of failing like an
            # unsupported HTTP route. We surface the same clean 404 shape that
            # callers expect for non-REST endpoints.
            response = JSONResponse(
                status_code=404,
                content={"detail": "Not Found"},
            )
            await response(scope, receive, send)

    def _is_authorized(self, scope: Scope) -> bool:
        bearer = self.settings.mcp_bearer_token
        if not bearer:
            return True

        for header_name, header_value in scope.get("headers", []):
            if header_name.lower() == b"authorization":
                token = header_value.decode(errors="ignore").strip()
                if token.lower().startswith("bearer "):
                    token = token[7:].strip()
                return token == bearer
        return False


def build_streamable_mcp_manager(settings: Settings) -> StreamableHTTPSessionManager:
    security_settings = None
    allowed_hosts = settings.parsed_mcp_allowed_hosts
    allowed_origins = settings.parsed_mcp_allowed_origins

    if allowed_hosts or allowed_origins:
        security_settings = TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

    # Motivation vs Logic: wrap the stdio MCP server in StreamableHTTPSessionManager so Claude.ai
    # reuses the shared tool inventory over HTTP/SSE without duplicating the orchestration flow.
    return StreamableHTTPSessionManager(
        app=build_mcp_server(settings),
        json_response=settings.mcp_json_response,
        stateless=settings.mcp_stateless,
        security_settings=security_settings,
        retry_interval=settings.mcp_retry_interval_ms,
        session_idle_timeout=settings.mcp_session_idle_timeout_seconds,
    )


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

    mcp_manager = build_streamable_mcp_manager(resolved_settings)
    mcp_transport_app = McpTransportASGI(mcp_manager, resolved_settings)

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
            "mcp_transport": "streamable-http",
            "mcp_path": resolved_settings.mcp_path,
            "mcp_entrypoint": "uvicorn app.main:app (StreamableHTTPSessionManager at /mcp)",
            "mcp_auth_required": bool(resolved_settings.mcp_bearer_token),
            "mcp_session_idle_timeout_seconds": resolved_settings.mcp_session_idle_timeout_seconds,
        }

    @app.on_event("startup")
    async def _start_streamable_mcp_manager() -> None:
        context = mcp_manager.run()
        app.state.mcp_session_manager_context = context
        await context.__aenter__()

    @app.on_event("shutdown")
    async def _stop_streamable_mcp_manager() -> None:
        context = getattr(app.state, "mcp_session_manager_context", None)
        if context is not None:
            await context.__aexit__(None, None, None)

    app.mount(resolved_settings.mcp_path, mcp_transport_app, name="mcp-transport")

    return app


app = create_app()
