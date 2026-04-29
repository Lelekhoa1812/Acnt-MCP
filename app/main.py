from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from app.auth import IdentityAuthError, IdentityGateway, reset_user_context, set_user_context
from app.auth import ClaudeOAuthService
from app.mcp.server import build_mcp_server

from app.api.routes.agent import router as agent_router
from app.api.routes.oauth import router as oauth_router
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


_OAUTH_CLIENT_NAMESPACE = "oauth_client"


def _oauth_client_storage_key(client_id: str) -> str:
    return client_id


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

    scopes_supported = ["openid", "profile", "email", "offline_access"]
    oauth_scope = settings.resolved_oauth_scope()
    if oauth_scope and oauth_scope not in scopes_supported:
        scopes_supported.append(oauth_scope)

    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/login",
        "token_endpoint": f"{base_url}/oauth/callback",
        "token_validation_endpoint": f"{base_url}/oauth/token/validate",
        "registration_endpoint": f"{base_url}/oauth/register",
        # Motivation vs Logic: Claude's browser login now uses a static client
        # registration, so discovery should advertise the login/callback flow and
        # keep the client-metadata document path disabled. The server still
        # supports DCR for clients like Cursor that expect a registration step.
        "client_id_metadata_document_supported": False,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": scopes_supported,
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


def _redirect_host_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    parsed = urlparse(uri)
    return parsed.hostname.lower() if parsed.hostname else None


async def _load_oauth_client(request: Request, client_id: str) -> dict[str, object] | None:
    cached = request.app.state.oauth_clients.get(client_id)
    if cached is not None:
        return cached

    container = getattr(request.app.state, "container", None)
    if container is None:
        return None

    record, _ = await container.key_value_store.get_json(_OAUTH_CLIENT_NAMESPACE, _oauth_client_storage_key(client_id))
    if isinstance(record, dict):
        request.app.state.oauth_clients[client_id] = record
        return record
    return None


async def _persist_oauth_client(request: Request, client_id: str, record: dict[str, object]) -> None:
    container = getattr(request.app.state, "container", None)
    if container is None:
        request.app.state.oauth_clients[client_id] = record
        return

    await container.key_value_store.set_json(
        _OAUTH_CLIENT_NAMESPACE,
        _oauth_client_storage_key(client_id),
        record,
        ttl_seconds=None,
    )
    request.app.state.oauth_clients[client_id] = record


def _registration_client_uri(base_url: str, client_id: str) -> str:
    return f"{base_url}/oauth/register/{client_id}"


def _oauth_registration_response(base_url: str, client_id: str, record: dict[str, object]) -> dict[str, object]:
    return {
        "client_id": client_id,
        "client_secret": record["client_secret"],
        "client_id_issued_at": record["client_id_issued_at"],
        "client_secret_expires_at": record["client_secret_expires_at"],
        "registration_client_uri": _registration_client_uri(base_url, client_id),
        "registration_access_token": record["registration_access_token"],
        "token_endpoint_auth_method": record["token_endpoint_auth_method"],
        "grant_types": record["grant_types"],
        "response_types": record["response_types"],
        "scope": record["scope"],
        "client_name": record.get("client_name"),
        "redirect_uris": record.get("redirect_uris", []),
        "auto_registered": record.get("auto_registered", False),
    }


def _bridge_access_token_payload(base_url: str, settings: Settings, client_id: str, grant_type: str) -> dict[str, object]:
    now = int(time.time())
    audience = settings.auth_audience or _mcp_resource_url(base_url, settings)
    issuer = settings.auth_issuer or base_url
    required_group = settings.auth_required_group.strip()
    groups = [required_group] if required_group else []
    roles = ["Tool.Viewer"]
    return {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + settings.mcp_oauth_token_ttl_seconds,
        "ver": settings.auth_required_token_version or "2.0",
        "tid": "mcp-bridge",
        "oid": client_id,
        "sub": client_id,
        "azp": client_id,
        "client_id": client_id,
        "grant_type": grant_type,
        "scope": "mcp",
        "groups": groups,
        "roles": roles,
        "token_origin": "mcp_oauth_bridge",
    }


def _bridge_access_token(base_url: str, settings: Settings, client_id: str, grant_type: str) -> str:
    secret = settings.mcp_oauth_jwt_signing_secret
    if not secret:
        raise RuntimeError("MCP OAuth bridge token signing secret is not configured.")
    payload = _bridge_access_token_payload(base_url, settings, client_id, grant_type)
    return jwt.encode(payload, secret, algorithm="HS256")


async def _auto_register_oauth_client(
    request: Request,
    client_id: str,
    redirect_uri: str | None,
    settings: Settings,
    logger: logging.Logger,
) -> bool:
    allowed_domains = settings.parsed_mcp_oauth_auto_trusted_redirect_domains
    host = _redirect_host_from_uri(redirect_uri)
    if not host or not allowed_domains:
        return False

    for domain in allowed_domains:
        if host == domain or host.endswith(f".{domain}"):
            # Motivation vs Logic: Claude.ai/ChatGPT connectors use variable redirect URIs,
            # so we auto-register any client_id that comes from those hosts instead of
            # forcing a manual POST before `/oauth/authorize`.
            record = {
                "client_id": client_id,
                "client_secret": secrets.token_urlsafe(32),
                "registration_access_token": secrets.token_urlsafe(32),
                "client_name": f"auto-{host}",
                "redirect_uris": [redirect_uri] if redirect_uri else [],
                "auto_registered": True,
                "client_id_issued_at": int(time.time()),
                "client_secret_expires_at": 0,
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "client_credentials"],
                "response_types": ["code"],
                "scope": "mcp",
            }
            await _persist_oauth_client(request, client_id, record)
            logger.info(
                "auto_registered_oauth_client client_id=%s redirect_uri=%s trusted_host=%s",
                client_id,
                redirect_uri,
                host,
            )
            return True

    return False


class McpTransportASGI:
    def __init__(
        self,
        manager: StreamableHTTPSessionManager,
        settings: Settings,
        identity_gateway: IdentityGateway,
    ) -> None:
        self.manager = manager
        self.settings = settings
        self.identity_gateway = identity_gateway

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            response = JSONResponse(
                status_code=405,
                content={"detail": "MCP transports only accept HTTP(S) requests."},
            )
            await response(scope, receive, send)
            return

        user_context = None
        if self.identity_gateway.enabled:
            try:
                user_context = self.identity_gateway.authenticate_headers(self._scope_headers(scope))
            except IdentityAuthError as exc:
                response = self._auth_error_response(scope, exc)
                await response(scope, receive, send)
                return
        elif self.settings.mcp_require_bearer_token and not self._is_authorized(scope):
            response = self._legacy_bearer_auth_error_response(scope)
            await response(scope, receive, send)
            return

        context_token = set_user_context(user_context) if user_context is not None else None
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
        finally:
            if context_token is not None:
                reset_user_context(context_token)

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

    def _scope_headers(self, scope: Scope) -> dict[str, str]:
        return {
            header_name.decode(errors="ignore").lower(): header_value.decode(errors="ignore")
            for header_name, header_value in scope.get("headers", [])
        }

    def _legacy_bearer_auth_error_response(self, scope: Scope) -> JSONResponse:
        base_url = _base_url_from_scope(scope, self.settings)
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid HTH_MCP_BEARER_TOKEN."},
            headers={
                "WWW-Authenticate": (
                    'Bearer realm="mcp", resource_metadata="'
                    f'{base_url}/.well-known/oauth-protected-resource"'
                )
            },
        )

    def _auth_error_response(self, scope: Scope, exc: IdentityAuthError) -> JSONResponse:
        base_url = _base_url_from_scope(scope, self.settings)
        headers: dict[str, str] = {}
        if exc.payload.status_code == 401:
            headers["WWW-Authenticate"] = (
                'Bearer realm="mcp", error="invalid_token", resource_metadata="'
                f'{base_url}/.well-known/oauth-protected-resource"'
            )
        return JSONResponse(
            status_code=exc.payload.status_code,
            content=exc.to_response_payload(),
            headers=headers,
        )


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
    identity_gateway = IdentityGateway(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for note in resolved_settings.startup_notes():
            logger.warning("startup_note=%s", note)
        container = await build_container(resolved_settings)
        app.state.container = container
        app.state.claude_oauth_service = ClaudeOAuthService(resolved_settings)
        app.state.oauth_clients = {}
        app.state.oauth_codes = {}
        app.state.oauth_login_states = {}
        app.state.identity_gateway = identity_gateway
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

    allowed_origins = list(resolved_settings.parsed_mcp_allowed_origins)
    if resolved_settings.mcp_browser_oauth_enabled and "null" not in allowed_origins:
        # Root Cause vs Logic: opening the helper HTML directly from disk sends
        # `Origin: null`, which the browser treats as a cross-origin request.
        # Allowing that origin keeps the local registration helper usable
        # without forcing a separate dev server, while still honoring any
        # explicit allowlist already configured.
        allowed_origins.append("null")

    if allowed_origins:
        # Motivation vs Logic: browser-based MCP clients like Claude.ai must
        # read discovery, OAuth, and 401 challenge responses cross-origin. We
        # keep CORS narrowly scoped to the configured allowlist instead of
        # opening the whole app.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate"],
        )

    mcp_manager = build_streamable_mcp_manager(resolved_settings)
    mcp_transport_app = McpTransportASGI(mcp_manager, resolved_settings, identity_gateway)

    api_tools_router = build_tools_router(resolved_settings)

    app.include_router(system_router, prefix=resolved_settings.api_prefix, tags=["system"])
    app.include_router(agent_router, prefix=resolved_settings.api_prefix, tags=["agent"])
    app.include_router(oauth_router, tags=["oauth"])
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
            "identity_auth_enabled": resolved_settings.identity_auth_enabled,
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

        base_url = _base_url_from_request(request, resolved_settings)
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        client_id = f"hth-{secrets.token_urlsafe(16)}"
        client_secret = secrets.token_urlsafe(32)
        registration_access_token = secrets.token_urlsafe(32)
        record = {
            "client_id": client_id,
            "client_secret": client_secret,
            "registration_access_token": registration_access_token,
            "client_name": body.get("client_name") if isinstance(body, dict) else None,
            "redirect_uris": body.get("redirect_uris", []) if isinstance(body, dict) else [],
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "client_credentials"],
            "response_types": ["code"],
            "scope": "mcp",
            "auto_registered": False,
        }
        await _persist_oauth_client(request, client_id, record)
        return JSONResponse(
            status_code=201,
            content=_oauth_registration_response(base_url, client_id, record),
        )

    @app.get("/oauth/register/{client_id}")
    async def oauth_get_registration(request: Request, client_id: str) -> JSONResponse:
        if not resolved_settings.mcp_browser_oauth_enabled:
            return JSONResponse(status_code=404, content={"detail": "MCP OAuth is disabled."})

        base_url = _base_url_from_request(request, resolved_settings)
        record = await _load_oauth_client(request, client_id)
        if record is None:
            return JSONResponse(status_code=404, content={"detail": "OAuth client not found."})

        registration_access_token = record.get("registration_access_token")
        headers = request.headers
        raw_auth = headers.get("authorization", "").strip()
        if raw_auth.lower().startswith("bearer "):
            raw_auth = raw_auth[7:].strip()
        if not raw_auth or not secrets.compare_digest(str(registration_access_token), raw_auth):
            return JSONResponse(status_code=401, content={"detail": "Invalid registration access token."})

        return JSONResponse(content=_oauth_registration_response(base_url, client_id, record))

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
        if client_id not in request.app.state.oauth_clients and await _load_oauth_client(request, client_id) is None:
            if not await _auto_register_oauth_client(
                request,
                client_id,
                redirect_uri,
                resolved_settings,
                logger,
            ):
                return JSONResponse(status_code=400, content={"error": "invalid_client"})

        # Motivation vs Logic: Claude.ai web connectors require a standards-shaped
        # OAuth handshake before they will send a bearer token to remote MCP.
        # This bridge issues short-lived authorization codes that redeem to a
        # server-signed JWT, keeping the connector flow self-contained while
        # still letting the MCP transport validate the same access token.
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

        base_url = _base_url_from_request(request, resolved_settings)
        body = (await request.body()).decode()
        params = {key: values[-1] for key, values in parse_qs(body).items()}
        grant_type = params.get("grant_type")
        client_id: str | None = params.get("client_id")

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
            client_id = str(code_record["client_id"])
        elif grant_type == "client_credentials":
            client = request.app.state.oauth_clients.get(client_id) if client_id else None
            if client is None and client_id is not None:
                client = await _load_oauth_client(request, client_id)
            if not client or not secrets.compare_digest(client["client_secret"], params.get("client_secret", "")):
                return JSONResponse(status_code=401, content={"error": "invalid_client"})
        else:
            return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})

        return JSONResponse(
            content={
                # Root Cause vs Logic: the connector flow must mint a real JWT so
                # the MCP transport can validate the same access token it receives
                # instead of failing on an opaque bearer string.
                "access_token": _bridge_access_token(base_url, resolved_settings, client_id or "", grant_type),
                "token_type": "Bearer",
                "expires_in": resolved_settings.mcp_oauth_token_ttl_seconds,
                "scope": "mcp",
            }
        )

    app.mount(resolved_settings.mcp_path, mcp_transport_app, name="mcp-transport")

    return app


app = create_app()
