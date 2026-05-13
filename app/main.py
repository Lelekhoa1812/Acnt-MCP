from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from app.api.routes.system import router as system_router
from app.api.routes.tools import build_tools_router
from app.config import (
    ParameterMappingError,
    Settings,
    UnsupportedToolError,
    UpstreamServiceError,
    build_container,
    get_settings,
)
from app.config.logging import configure_logging
from app.mcp.server import build_mcp_server


def build_streamable_mcp_manager(settings: Settings) -> StreamableHTTPSessionManager:
    security_settings = None
    allowed_hosts = settings.parsed_mcp_allowed_hosts
    allowed_origins = settings.parsed_mcp_allowed_origins
    if allowed_hosts or allowed_origins:
        security_settings = TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
    # session_idle_timeout is not supported in stateless mode
    session_timeout = None if settings.mcp_stateless else settings.mcp_session_idle_timeout_seconds

    return StreamableHTTPSessionManager(
        app=build_mcp_server(settings),
        json_response=settings.mcp_json_response,
        stateless=settings.mcp_stateless,
        security_settings=security_settings,
        retry_interval=settings.mcp_retry_interval_ms,
        session_idle_timeout=session_timeout,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    logger = logging.getLogger("acnt")

    mcp_manager = build_streamable_mcp_manager(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for note in resolved_settings.startup_notes():
            logger.warning("startup_note=%s", note)
        container = await build_container(resolved_settings)
        app.state.container = container
        mcp_context = mcp_manager.run()
        await mcp_context.__aenter__()
        try:
            yield
        finally:
            await mcp_context.__aexit__(None, None, None)
            await container.close()

    app = FastAPI(
        title=resolved_settings.server_name,
        version=resolved_settings.server_version,
        lifespan=lifespan,
    )

    allowed_origins = resolved_settings.parsed_mcp_allowed_origins
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    app.include_router(system_router, prefix=resolved_settings.api_prefix, tags=["system"])
    app.include_router(build_tools_router(), prefix=resolved_settings.api_prefix, tags=["tools"])

    @app.exception_handler(ParameterMappingError)
    async def handle_parameter_mapping_error(_: Request, exc: ParameterMappingError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(UnsupportedToolError)
    async def handle_unsupported_tool(_: Request, exc: UnsupportedToolError) -> JSONResponse:
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
            "api_prefix": resolved_settings.api_prefix,
            "mcp_transport": "streamable-http",
            "mcp_path": resolved_settings.mcp_path,
        }

    app.mount(resolved_settings.mcp_path, _StreamableMcpAsgi(mcp_manager), name="mcp-transport")
    return app


class _StreamableMcpAsgi:
    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            response = JSONResponse(
                status_code=405,
                content={"detail": "MCP transports only accept HTTP(S) requests."},
            )
            await response(scope, receive, send)
            return
        try:
            await self.manager.handle_request(scope, receive, send)
        except RuntimeError as exc:
            if "Task group is not initialized" not in str(exc):
                raise
            response = JSONResponse(status_code=404, content={"detail": "Not Found"})
            await response(scope, receive, send)


app = create_app()
