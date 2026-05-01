from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from time import time
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from app.auth.claims import resolve_user_email
from app.auth.models import UserContext
from app.config.settings import Settings


@dataclass(frozen=True)
class AuthFailurePayload:
    code: str
    message: str
    status_code: int
    missing_claims: list[str]
    mcp_error_code: int | None = None


class IdentityAuthError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        missing_claims: list[str] | None = None,
        mcp_error_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = AuthFailurePayload(
            code=code,
            message=message,
            status_code=status_code,
            missing_claims=missing_claims or [],
            mcp_error_code=mcp_error_code,
        )

    def to_response_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.payload.code,
                "message": self.payload.message,
            }
        }
        if self.payload.missing_claims:
            body["error"]["missing_claims"] = self.payload.missing_claims
        if self.payload.mcp_error_code is not None:
            body["error"]["mcp_error_code"] = self.payload.mcp_error_code
        return body


class IdentityGateway:
    # Motivation vs Logic: this gateway centralizes JWT verification, ABAC/RBAC
    # checks, and per-user throttling so both REST and MCP transports enforce the
    # same identity contract with one implementation.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwk_client = PyJWKClient(settings.auth_jwks_url) if settings.auth_jwks_url else None
        self._rate_limit_lock = Lock()
        self._rate_limit_windows: defaultdict[str, deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return self.settings.identity_auth_enabled

    def authenticate_headers(self, headers: Mapping[str, str]) -> UserContext:
        token = self._extract_bearer_token(headers)
        claims = self._decode_token(token)
        user_context = self.authorize_claims(claims)
        self._enforce_rate_limit(user_context)
        return user_context

    def authorize_claims(self, claims: dict[str, Any]) -> UserContext:
        # Motivation vs Logic: OAuth bridge callbacks already validate the
        # Entra JWT before copying claims into a bridge token. Reusing the same
        # authorization path here lets the token endpoint reject non-members
        # before issuing MCP credentials, while `/mcp` remains the final gate.
        user_context = self._build_user_context(claims)
        self._enforce_identity_gating(user_context)
        return user_context

    def _extract_bearer_token(self, headers: Mapping[str, str]) -> str:
        raw = headers.get("authorization")
        if not raw:
            raise IdentityAuthError(
                code="missing_bearer_token",
                message="Authorization bearer token is required.",
                status_code=401,
                mcp_error_code=-32001,
            )
        token = raw.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise IdentityAuthError(
                code="invalid_bearer_token",
                message="Authorization bearer token is empty.",
                status_code=401,
                mcp_error_code=-32001,
            )
        return token

    def _decode_token(self, token: str) -> dict[str, Any]:
        audience = self.settings.auth_audience or None
        issuer = self.settings.auth_issuer or None
        last_error: Exception | None = None
        if self.settings.auth_jwt_hs256_secret:
            try:
                return jwt.decode(
                    token,
                    self.settings.auth_jwt_hs256_secret,
                    algorithms=["HS256"],
                    audience=audience,
                    issuer=issuer,
                    options={"require": ["exp"]},
                )
            except InvalidTokenError as exc:
                last_error = exc

        if self._jwk_client is not None:
            try:
                signing_key = self._jwk_client.get_signing_key_from_jwt(token).key
                return jwt.decode(
                    token,
                    signing_key,
                    algorithms=["RS256", "RS384", "RS512"],
                    audience=audience,
                    issuer=issuer,
                    options={"require": ["exp"]},
                )
            except (InvalidTokenError, PyJWKClientError) as exc:
                last_error = exc

        bridge_secret = self.settings.mcp_oauth_jwt_signing_secret
        if bridge_secret:
            try:
                bridge_claims = jwt.decode(
                    token,
                    bridge_secret,
                    algorithms=["HS256"],
                    audience=audience,
                    issuer=self.settings.mcp_oauth_bridge_issuer,
                    options={"require": ["exp"]},
                )
                if bridge_claims.get("token_origin") == "mcp_oauth_bridge":
                    return bridge_claims
                last_error = InvalidTokenError("bridge token missing mcp_oauth_bridge origin")
            except InvalidTokenError as exc:
                last_error = exc

        if last_error is not None:
            raise IdentityAuthError(
                code="invalid_token",
                message=f"Token validation failed: {last_error}",
                status_code=401,
                mcp_error_code=-32001,
            ) from last_error

        raise IdentityAuthError(
            code="identity_config_error",
            message=(
                "Identity auth is enabled but no verifier is configured. Set HTH_AUTH_JWKS_URL "
                "or HTH_AUTH_JWT_HS256_SECRET."
            ),
            status_code=500,
        )

    def _build_user_context(self, claims: dict[str, Any]) -> UserContext:
        if claims.get("token_origin") == "mcp_oauth_bridge" and claims.get("grant_type") == "client_credentials":
            raise IdentityAuthError(
                code="app_only_token_denied",
                message="Client credentials tokens cannot access user-scoped MCP tools.",
                status_code=403,
            )

        required_claims = self.settings.parsed_auth_required_claims
        missing_claims = [claim for claim in required_claims if not claims.get(claim)]
        if missing_claims:
            raise IdentityAuthError(
                code="missing_claims",
                message="Token is missing required identity claims.",
                status_code=403,
                missing_claims=missing_claims,
            )

        if self.settings.auth_required_token_version:
            token_version = str(claims.get("ver") or claims.get("token_version") or "")
            if token_version != self.settings.auth_required_token_version:
                raise IdentityAuthError(
                    code="unsupported_token_version",
                    message=(
                        "Token version mismatch. "
                        f"Expected {self.settings.auth_required_token_version}, got {token_version or 'missing'}."
                    ),
                    status_code=403,
                )

        tenant_id = str(claims.get("tid") or "")
        oid = str(claims.get("oid") or "").strip() or None
        user_id = str(claims.get("oid") or claims.get("sub") or "")
        subject = str(claims.get("sub") or user_id)
        if not tenant_id or not user_id:
            raise IdentityAuthError(
                code="thin_token",
                message="Token must include both tid and oid/sub claims.",
                status_code=403,
                missing_claims=["tid", "oid"],
            )

        user_context = UserContext(
            tenant_id=tenant_id,
            user_id=user_id,
            subject=subject,
            oid=oid,
            email=resolve_user_email(claims),
            client_id=str(claims.get("azp") or claims.get("appid") or claims.get("client_id") or "").strip() or None,
            token_origin=str(claims.get("token_origin") or "").strip() or None,
            roles=self._normalize_claim_values(claims.get("roles")),
            groups=self._normalize_claim_values(claims.get("groups")),
            group_names=self._normalize_claim_values(claims.get("group_names")),
            claims=claims,
        )
        claim_plugin_permissions = self._normalize_claim_values(claims.get("plugin_permissions"))
        user_context.plugin_permissions = (
            claim_plugin_permissions if claim_plugin_permissions else self.resolve_allowed_plugins(user_context)
        )
        return user_context

    def _enforce_identity_gating(self, user_context: UserContext) -> None:
        required_groups = self.settings.parsed_oauth_user_groups
        if not required_groups:
            return
        if self._configured_groups_match_user(required_groups, user_context):
            return
        required_label = ", ".join(sorted(required_groups, key=str.casefold)) or "<none>"
        token_group_label = ", ".join(sorted(user_context.groups, key=str.casefold)) or "<none>"
        token_name_label = ", ".join(sorted(user_context.group_names, key=str.casefold)) or "<none>"
        raise IdentityAuthError(
            code="group_access_denied",
            message=(
                f"User is not in required group '{required_label}'. "
                f"Token group ids were [{token_group_label}] and group names were [{token_name_label}]. "
                "This app compares the signed-in user's delegated Microsoft Graph memberships against configured "
                "group IDs or display names; it does not enumerate configured group members."
            ),
            status_code=403,
        )

    def clear_group_caches(self) -> None:
        return None

    @staticmethod
    def _looks_like_guid(value: str) -> bool:
        parts = value.split("-")
        return len(parts) == 5 and [len(part) for part in parts] == [8, 4, 4, 4, 12]

    def resolve_allowed_plugins(self, user_context: UserContext) -> list[str]:
        # Motivation vs Logic: MCP access and plugin/tool access have different
        # operators now. Resolve plugin groups once per authenticated request so
        # discovery, direct calls, and planner prompts share the same allow-list.
        allowed: list[str] = []
        for plugin in ("news", "weather", "currency", "stock"):
            if self.user_can_access_plugin(user_context, plugin):
                allowed.append(plugin)
        return allowed

    def user_can_access_plugin(self, user_context: UserContext, plugin: str) -> bool:
        group_names = self.settings.parsed_plugin_groups(plugin)
        if self._plugin_group_policy_all(group_names):
            return True
        return self._configured_groups_match_user(group_names, user_context)

    @staticmethod
    def _plugin_group_policy_all(group_names: list[str]) -> bool:
        if not group_names:
            return True
        return any(group.casefold() == "all" for group in group_names)

    @staticmethod
    def _configured_groups_match_user(configured_groups: list[str], user_context: UserContext) -> bool:
        user_group_ids = IdentityGateway._normalized_set(user_context.groups)
        user_group_names = IdentityGateway._normalized_set(user_context.group_names)
        for configured_group in configured_groups:
            configured = configured_group.strip()
            if not configured:
                continue
            normalized = configured.casefold()
            if IdentityGateway._looks_like_guid(configured):
                if normalized in user_group_ids:
                    return True
                continue
            if normalized in user_group_names or normalized in user_group_ids:
                return True
        return False

    @staticmethod
    def _normalized_set(values: list[str]) -> set[str]:
        return {value.strip().casefold() for value in values if value.strip()}

    # Department-based access is disabled for now, so the old claim lookup is
    # kept here as commented reference only.
    # def _resolve_department_claim(self, claims: dict[str, Any]) -> str | None:
    #     for claim_name in self.settings.parsed_auth_department_claims:
    #         value = claims.get(claim_name)
    #         if value is None:
    #             continue
    #         if isinstance(value, str) and value.strip():
    #             return value.strip()
    #         if isinstance(value, (int, float)):
    #             return str(value)
    #     return None

    def _normalize_claim_values(self, raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [item for item in (part.strip() for part in raw.replace(",", " ").split()) if item]
        if isinstance(raw, list):
            values: list[str] = []
            for item in raw:
                if item is None:
                    continue
                rendered = str(item).strip()
                if rendered:
                    values.append(rendered)
            return values
        return [str(raw).strip()] if str(raw).strip() else []

    def _enforce_rate_limit(self, user_context: UserContext) -> None:
        per_minute = self.settings.auth_rate_limit_per_minute
        if per_minute <= 0:
            return

        now = time()
        window_start = now - 60.0
        key = f"{user_context.tenant_id}:{user_context.user_id}"

        with self._rate_limit_lock:
            calls = self._rate_limit_windows[key]
            while calls and calls[0] < window_start:
                calls.popleft()
            if len(calls) >= per_minute:
                raise IdentityAuthError(
                    code="rate_limited",
                    message=f"Rate limit exceeded ({per_minute} calls/minute).",
                    status_code=429,
                )
            calls.append(now)
