from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from time import time
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

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
        user_context = self._build_user_context(claims)
        self._enforce_identity_gating(user_context)
        self._enforce_rate_limit(user_context)
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
                raise IdentityAuthError(
                    code="invalid_token",
                    message=f"Token validation failed: {exc}",
                    status_code=401,
                    mcp_error_code=-32001,
                ) from exc

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
            except (InvalidTokenError, PyJWKClientError):
                pass

        bridge_secret = self.settings.mcp_oauth_jwt_signing_secret
        if bridge_secret:
            try:
                return jwt.decode(
                    token,
                    bridge_secret,
                    algorithms=["HS256"],
                    audience=audience,
                    issuer=issuer,
                    options={"require": ["exp"]},
                )
            except InvalidTokenError as exc:
                raise IdentityAuthError(
                    code="invalid_token",
                    message=f"Token validation failed: {exc}",
                    status_code=401,
                    mcp_error_code=-32001,
                ) from exc

        raise IdentityAuthError(
            code="identity_config_error",
            message=(
                "Identity auth is enabled but no verifier is configured. Set HTH_AUTH_JWKS_URL "
                "or HTH_AUTH_JWT_HS256_SECRET."
            ),
            status_code=500,
        )

    def _build_user_context(self, claims: dict[str, Any]) -> UserContext:
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
        user_id = str(claims.get("oid") or claims.get("sub") or "")
        subject = str(claims.get("sub") or user_id)
        if not tenant_id or not user_id:
            raise IdentityAuthError(
                code="thin_token",
                message="Token must include both tid and oid/sub claims.",
                status_code=403,
                missing_claims=["tid", "oid"],
            )

        return UserContext(
            tenant_id=tenant_id,
            user_id=user_id,
            subject=subject,
            email=self._resolve_user_email(claims),
            roles=self._normalize_claim_values(claims.get("roles")),
            groups=self._normalize_claim_values(claims.get("groups")),
            claims=claims,
        )

    def _resolve_user_email(self, claims: dict[str, Any]) -> str | None:
        # Motivation vs Logic: different connectors and identity providers expose
        # different email-shaped claims, so we normalize the first usable value
        # once here and let the rest of the system log the same identity string.
        for claim_name in ("email", "preferred_username", "upn", "unique_name"):
            raw_value = claims.get(claim_name)
            if raw_value is None:
                continue
            rendered_value = raw_value.strip() if isinstance(raw_value, str) else str(raw_value).strip()
            if rendered_value:
                return rendered_value
        return None

    def _enforce_identity_gating(self, user_context: UserContext) -> None:
        required_group = self.settings.auth_required_group.strip()
        if not required_group:
            return
        if required_group not in set(user_context.groups):
            raise IdentityAuthError(
                code="group_access_denied",
                message=f"User is not in required group '{required_group}'.",
                status_code=403,
            )

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
