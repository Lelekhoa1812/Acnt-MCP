from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from app.config.settings import Settings


@dataclass(frozen=True)
class ClaudeOAuthError(Exception):
    code: str
    message: str
    status_code: int = 400


class ClaudeOAuthService:
    # Motivation vs Logic: Claude needs a dedicated OAuth client flow that uses
    # the project's static client credentials, while the MCP transport keeps
    # using the phase-2 bridge as the default public connector contract. This
    # service owns the optional direct Entra browser redirect, code exchange,
    # and token validation concerns in one place.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        jwks_url = settings.resolved_oauth_jwks_url
        self._jwk_client = PyJWKClient(jwks_url) if jwks_url else None

    @property
    def enabled(self) -> bool:
        return self.settings.claude_oauth_enabled

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ClaudeOAuthError(
                code="oauth_not_configured",
                message=(
                    "Claude OAuth login is not configured. Set OAUTH_CLIENT_ID and either "
                    "OAUTH_AUTHORITY or OAUTH_TENANT_ID."
                ),
                status_code=500,
            )

    def _require_validation_enabled(self) -> None:
        if not (
            self.settings.oauth_client_id
            and (self.settings.oauth_authority or self.settings.oauth_tenant_id or self.settings.auth_issuer)
        ):
            raise ClaudeOAuthError(
                code="oauth_validation_not_configured",
                message="OAuth token validation requires OAUTH_CLIENT_ID and OAUTH_TENANT_ID or OAUTH_AUTHORITY.",
                status_code=500,
            )

    def _require_authority(self) -> str:
        authority = self.settings.resolved_oauth_authority
        if not authority:
            raise ClaudeOAuthError(
                code="oauth_authority_missing",
                message="Claude OAuth authority is missing. Set OAUTH_AUTHORITY or OAUTH_TENANT_ID.",
                status_code=500,
            )
        return authority

    def _require_redirect_uri(self, base_url: str) -> str:
        redirect_uri = self.settings.oauth_redirect_uri
        if redirect_uri:
            return redirect_uri
        return f"{base_url.rstrip('/')}/oauth/callback"

    @staticmethod
    def _pkce_code_verifier() -> str:
        return secrets.token_urlsafe(64)

    @staticmethod
    def _pkce_code_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _login_scope(self) -> str:
        graph_scopes = self.settings.parsed_oauth_graph_scopes
        if not graph_scopes:
            raise ClaudeOAuthError(
                code="oauth_scope_missing",
                message="OAuth Graph scopes are missing. Set OAUTH_GRAPH_SCOPES.",
                status_code=500,
            )
        return " ".join(["openid", "profile", "email", "offline_access", *graph_scopes])

    def build_authorize_url(self, *, base_url: str, state: str) -> tuple[str, str]:
        self._require_enabled()
        authorize_url = self.settings.resolved_oauth_authorize_url
        if not authorize_url:
            raise ClaudeOAuthError(
                code="oauth_authorize_url_missing",
                message="Claude OAuth authorize URL could not be resolved.",
                status_code=500,
            )

        # Root Cause vs Logic: Azure rejects cross-origin authorization code
        # redemption unless the flow uses PKCE. We generate the verifier here
        # so the login redirect and callback exchange share the same proof key.
        code_verifier = self._pkce_code_verifier()

        params = {
            "client_id": self.settings.oauth_client_id,
            "response_type": "code",
            "redirect_uri": self._require_redirect_uri(base_url),
            "response_mode": "query",
            "scope": self._login_scope(),
            "state": state,
            "code_challenge": self._pkce_code_challenge(code_verifier),
            "code_challenge_method": "S256",
            # Root Cause vs Logic: Entra can silently reuse an existing browser
            # SSO session, which makes connector auth look like it skipped login
            # and may bind the MCP grant to the wrong cached Microsoft account.
            "prompt": "select_account",
        }
        return f"{authorize_url}?{urlencode(params)}", code_verifier

    async def exchange_code_for_token(self, *, base_url: str, code: str, code_verifier: str | None = None) -> dict[str, Any]:
        self._require_enabled()
        token_url = self.settings.resolved_oauth_token_url
        if not token_url:
            raise ClaudeOAuthError(
                code="oauth_token_url_missing",
                message="Claude OAuth token URL could not be resolved.",
                status_code=500,
            )

        # Root Cause vs Logic: deployments may use either a SPA/public Entra
        # client or a confidential web client. The auth method setting keeps
        # PKCE in both cases while only sending the secret for web-client apps.
        data = {
            "client_id": self.settings.oauth_client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._require_redirect_uri(base_url),
            "scope": self._login_scope(),
        }
        if (
            self.settings.resolved_oauth_client_auth_method == "client_secret_post"
            and self.settings.oauth_client_secret
        ):
            data["client_secret"] = self.settings.oauth_client_secret
        if code_verifier:
            data["code_verifier"] = code_verifier

        headers = {
            # Root Cause vs Logic: SPA-style Entra registrations require a
            # browser origin for code redemption; confidential clients ignore it.
            "Origin": base_url.rstrip("/"),
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(token_url, data=data, headers=headers)

        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {"error": "token_exchange_failed", "error_description": response.text}
            raise ClaudeOAuthError(
                code=str(error_payload.get("error") or "token_exchange_failed"),
                message=str(error_payload.get("error_description") or "Claude OAuth token exchange failed."),
                status_code=response.status_code,
            )

        payload = response.json()
        if "access_token" not in payload:
            raise ClaudeOAuthError(
                code="missing_access_token",
                message="The OAuth token response did not include an access token.",
                status_code=502,
            )
        return payload

    async def fetch_user_group_memberships(self, access_token: str) -> dict[str, list[str]]:
        # Motivation vs Logic: authorization should follow the signed-in user,
        # not a configured group. The delegated Graph token can read only this
        # user's transitive memberships, which lets us compare local allow-lists
        # without enumerating group members or using app-only directory access.
        ids: list[str] = []
        names: list[str] = []
        url: str | None = "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group"
        params: dict[str, str] | None = {"$select": "id,displayName", "$top": "999"}
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient(timeout=20.0) as client:
            while url:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code >= 400:
                    try:
                        error_payload = response.json()
                    except ValueError:
                        error_payload = {"error": "graph_membership_lookup_failed", "error_description": response.text}
                    raise ClaudeOAuthError(
                        code=str(error_payload.get("error") or "graph_membership_lookup_failed"),
                        message=(
                            "Microsoft Graph delegated user membership lookup failed. "
                            "Ensure the Entra app has admin-consented delegated Graph membership scopes."
                        ),
                        status_code=response.status_code,
                    )
                payload = response.json()
                values = payload.get("value")
                if isinstance(values, list):
                    for item in values:
                        if not isinstance(item, dict):
                            continue
                        group_id = str(item.get("id") or "").strip()
                        display_name = str(item.get("displayName") or "").strip()
                        if group_id:
                            ids.append(group_id)
                        if display_name:
                            names.append(display_name)
                next_link = payload.get("@odata.nextLink")
                url = str(next_link).strip() if next_link else None
                params = None

        return {"groups": self._dedupe(ids), "group_names": self._dedupe(names)}

    def validate_access_token(self, token: str) -> dict[str, Any]:
        self._require_validation_enabled()
        issuer = self.settings.resolved_oauth_issuer
        audiences = self.settings.resolved_oauth_audience_variants()
        if not issuer or not audiences:
            raise ClaudeOAuthError(
                code="oauth_validation_config_missing",
                message="Claude OAuth validation settings are incomplete.",
                status_code=500,
            )

        try:
            if self._jwk_client is None:
                raise ClaudeOAuthError(
                    code="oauth_jwks_missing",
                    message="Claude OAuth JWKS endpoint is not configured.",
                    status_code=500,
                )
            signing_key = self._jwk_client.get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                signing_key,
                algorithms=["RS256", "RS384", "RS512"],
                audience=audiences,
                issuer=issuer,
                options={"require": ["exp"]},
            )
        except ClaudeOAuthError:
            raise
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise ClaudeOAuthError(
                code="invalid_token",
                message=f"Token validation failed: {exc}",
                status_code=401,
            ) from exc

    def validate_id_token(self, token: str) -> dict[str, Any]:
        self._require_validation_enabled()
        issuer = self.settings.resolved_oauth_issuer
        audiences = [aud for aud in [self.settings.oauth_client_id, *self.settings.resolved_oauth_audience_variants()] if aud]
        if not issuer or not audiences:
            raise ClaudeOAuthError(
                code="oauth_validation_config_missing",
                message="OAuth ID token validation settings are incomplete.",
                status_code=500,
            )

        try:
            if self._jwk_client is None:
                raise ClaudeOAuthError(
                    code="oauth_jwks_missing",
                    message="OAuth JWKS endpoint is not configured.",
                    status_code=500,
                )
            signing_key = self._jwk_client.get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                signing_key,
                algorithms=["RS256", "RS384", "RS512"],
                audience=audiences,
                issuer=issuer,
                options={"require": ["exp"]},
            )
        except ClaudeOAuthError:
            raise
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise ClaudeOAuthError(
                code="invalid_token",
                message=f"ID token validation failed: {exc}",
                status_code=401,
            ) from exc

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped

    @staticmethod
    def extract_bearer_token(headers: dict[str, str]) -> str | None:
        raw = headers.get("authorization")
        if not raw:
            return None
        token = raw.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None
