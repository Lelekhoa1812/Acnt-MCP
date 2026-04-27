from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
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


def _base_url_from_scope(scope: Scope, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")

    headers = {name.lower(): value for name, value in scope.get("headers", [])}
    host = headers.get(b"host", b"localhost").decode(errors="ignore")
    forwarded_proto = headers.get(b"x-forwarded-proto")
    scheme = (
        forwarded_proto.decode(errors="ignore").split(",", 1)[0].strip()
        if forwarded_proto
        else str(scope.get("scheme") or "https")
    )
    return f"{scheme}://{host}".rstrip("/")


def _base_url_from_request(request: Request, settings: Settings) -> str:
    return _base_url_from_scope(request.scope, settings)


def _mcp_resource_url(base_url: str, settings: Settings) -> str:
    return f"{base_url}{settings.mcp_path}"


def _oauth_protected_resource_metadata(base_url: str, settings: Settings) -> dict[str, object]:
    return {
        "resource": _mcp_resource_url(base_url, settings),
        "authorization_servers": [base_url],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
        "resource_name": settings.server_name,
    }


def _oauth_authorization_server_metadata(base_url: str, settings: Settings) -> dict[str, object]:
    service_documentation = settings.resolved_server_website_url
    if service_documentation.startswith("/"):
        service_documentation = f"{base_url}{service_documentation}"

    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        # Motivation vs Logic: Claude.ai web connectors and other remote MCP
        # clients are more reliable when the auth server advertises CIMD support,
        # because they can register via a metadata URL instead of depending only
        # on dynamic registration semantics.
        "client_id_metadata_document_supported": True,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp"],
        "service_documentation": service_documentation,
    }


def _code_challenge_matches(code_challenge: str | None, method: str | None, verifier: str | None) -> bool:
    if not code_challenge:
        return True
    if not verifier:
        return False
    if method == "plain":
        return secrets.compare_digest(code_challenge, verifier)

    digest = hashlib.sha256(verifier.encode()).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(code_challenge, encoded)


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

        if not self.settings.mcp_require_bearer_token:
            await self.manager.handle_request(scope, receive, send)
            return

        if not self._is_authorized(scope):
            base_url = _base_url_from_scope(scope, self.settings)
            response = JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid HTH_MCP_BEARER_TOKEN."},
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="mcp", resource_metadata="'
                        f'{base_url}/.well-known/oauth-protected-resource"'
                    )
                },
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
        app.state.oauth_clients = {}
        app.state.oauth_codes = {}
        mcp_context = mcp_manager.run()
        app.state.mcp_session_manager_context = mcp_context
        await mcp_context.__aenter__()
        try:
            # Root Cause vs Logic: FastAPI ignores legacy startup events when a
            # lifespan handler is supplied, so the deployed `/mcp` mount was
            # accepting requests before the StreamableHTTP task group existed.
            # Starting the manager here keeps REST, UI, and MCP lifecycle in one
            # ASGI startup path.
            yield
        finally:
            await mcp_context.__aexit__(None, None, None)
            await container.close()

    app = FastAPI(
        title=resolved_settings.server_name,
        version=resolved_settings.server_version,
        lifespan=lifespan,
    )

    if resolved_settings.parsed_mcp_allowed_origins:
        # Motivation vs Logic: browser-based MCP clients like Claude.ai must
        # read discovery, OAuth, and 401 challenge responses cross-origin. We
        # keep CORS narrowly scoped to the configured allowlist instead of
        # opening the whole app.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.parsed_mcp_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate"],
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
        mock_ui_root_path = resolved_settings.resolve_path(resolved_settings.mock_ui_path).parent
        mock_ui_assets_path = mock_ui_root_path / "assets"
        if mock_ui_assets_path.exists():
            app.mount(
                f"{resolved_settings.api_prefix}/chat/assets",
                StaticFiles(directory=mock_ui_assets_path),
                name="chat-assets",
            )
            # Root Cause vs Logic: legacy paths still work for cached HTML or old bookmarks.
            app.mount(
                f"{resolved_settings.api_prefix}/ui/assets",
                StaticFiles(directory=mock_ui_assets_path),
                name="ui-assets-legacy",
            )
            app.mount(
                f"{resolved_settings.api_prefix}/mock-ui/assets",
                StaticFiles(directory=mock_ui_assets_path),
                name="mock-ui-assets-legacy",
            )
        mock_ui_public_path = mock_ui_root_path / "public"
        if mock_ui_public_path.exists():
            app.mount(
                f"{resolved_settings.api_prefix}/chat/public",
                StaticFiles(directory=mock_ui_public_path),
                name="chat-public",
            )
            app.mount(
                f"{resolved_settings.api_prefix}/ui/public",
                StaticFiles(directory=mock_ui_public_path),
                name="ui-public-legacy",
            )
            app.mount(
                f"{resolved_settings.api_prefix}/mock-ui/public",
                StaticFiles(directory=mock_ui_public_path),
                name="mock-ui-public-legacy",
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
            "mcp_auth_required": resolved_settings.mcp_require_bearer_token,
            "mcp_oauth_enabled": resolved_settings.mcp_browser_oauth_enabled,
            "mcp_session_idle_timeout_seconds": resolved_settings.mcp_session_idle_timeout_seconds,
            "logo_url": resolved_settings.resolved_server_logo_url,
        }

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/{_:path}")
    async def oauth_protected_resource(request: Request, _: str = "") -> dict[str, object]:
        base_url = _base_url_from_request(request, resolved_settings)
        return _oauth_protected_resource_metadata(base_url, resolved_settings)

    @app.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server(request: Request) -> dict[str, object]:
        base_url = _base_url_from_request(request, resolved_settings)
        return _oauth_authorization_server_metadata(base_url, resolved_settings)

    @app.post("/oauth/register")
    @app.post("/register")
    async def oauth_register(request: Request) -> JSONResponse:
        if not resolved_settings.mcp_browser_oauth_enabled:
            return JSONResponse(status_code=404, content={"detail": "MCP OAuth is disabled."})

        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        client_id = f"hth-{secrets.token_urlsafe(16)}"
        client_secret = secrets.token_urlsafe(32)
        request.app.state.oauth_clients[client_id] = {
            "client_secret": client_secret,
            "client_name": body.get("client_name") if isinstance(body, dict) else None,
            "redirect_uris": body.get("redirect_uris", []) if isinstance(body, dict) else [],
        }
        return JSONResponse(
            status_code=201,
            content={
                "client_id": client_id,
                "client_secret": client_secret,
                "client_id_issued_at": int(time.time()),
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "client_credentials"],
                "response_types": ["code"],
                "scope": "mcp",
            },
        )

    @app.get("/oauth/authorize")
    @app.get("/authorize")
    async def oauth_authorize(request: Request) -> Response:
        if not resolved_settings.mcp_browser_oauth_enabled:
            return JSONResponse(status_code=404, content={"detail": "MCP OAuth is disabled."})

        params = request.query_params
        client_id = params.get("client_id")
        redirect_uri = params.get("redirect_uri")
        response_type = params.get("response_type")
        if response_type != "code" or not client_id or not redirect_uri:
            return JSONResponse(status_code=400, content={"error": "invalid_request"})
        if client_id not in request.app.state.oauth_clients:
            return JSONResponse(status_code=400, content={"error": "invalid_client"})

        # Motivation vs Logic: Claude.ai web connectors require a standards-shaped
        # OAuth handshake before they will send a bearer token to remote MCP.
        # This bridge issues short-lived authorization codes that redeem to the
        # existing HTH_MCP_BEARER_TOKEN, keeping one server-side credential while
        # allowing OAuth-only clients to complete connector setup.
        code = secrets.token_urlsafe(32)
        request.app.state.oauth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": params.get("code_challenge"),
            "code_challenge_method": params.get("code_challenge_method") or "plain",
            "expires_at": time.time() + 300,
        }
        redirect_params = {"code": code}
        if params.get("state"):
            redirect_params["state"] = params["state"]
        return RedirectResponse(f"{redirect_uri}?{urlencode(redirect_params)}", status_code=302)

    @app.post("/oauth/token")
    @app.post("/token")
    async def oauth_token(request: Request) -> JSONResponse:
        if not resolved_settings.mcp_browser_oauth_enabled:
            return JSONResponse(status_code=404, content={"detail": "MCP OAuth is disabled."})

        body = (await request.body()).decode()
        params = {key: values[-1] for key, values in parse_qs(body).items()}
        grant_type = params.get("grant_type")

        if grant_type == "authorization_code":
            code = params.get("code")
            code_record = request.app.state.oauth_codes.pop(code, None) if code else None
            if not code_record or code_record["expires_at"] < time.time():
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
            if params.get("redirect_uri") and params["redirect_uri"] != code_record["redirect_uri"]:
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
            if params.get("client_id") and params["client_id"] != code_record["client_id"]:
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
            if not _code_challenge_matches(
                code_record.get("code_challenge"),
                code_record.get("code_challenge_method"),
                params.get("code_verifier"),
            ):
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
        elif grant_type == "client_credentials":
            client = request.app.state.oauth_clients.get(params.get("client_id"))
            if not client or not secrets.compare_digest(client["client_secret"], params.get("client_secret", "")):
                return JSONResponse(status_code=401, content={"error": "invalid_client"})
        else:
            return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})

        return JSONResponse(
            content={
                "access_token": resolved_settings.mcp_bearer_token,
                "token_type": "Bearer",
                "expires_in": resolved_settings.mcp_oauth_token_ttl_seconds,
                "scope": "mcp",
            }
        )

    app.mount(resolved_settings.mcp_path, mcp_transport_app, name="mcp-transport")

    return app


app = create_app()
