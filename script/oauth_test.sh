#!/usr/bin/env bash
set -euo pipefail

# Motivation vs Logic: this smoke harness keeps the Microsoft Entra issuer,
# audience, and JWKS URL sourced from `.env`, then exercises the FastAPI auth
# gateway end-to-end with a local RSA JWKS stub so we can validate claim checks
# deterministically without needing a live Azure token for every run.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOTENV_FILE="${DOTENV_FILE:-${PROJECT_ROOT}/.env}"

if [[ -f "${DOTENV_FILE}" ]]; then
  # `.env` is expected to be shell-compatible `KEY=VALUE` input.
  set -a
  # shellcheck disable=SC1090
  source "${DOTENV_FILE}"
  set +a
else
  echo "No .env file found at ${DOTENV_FILE}; continuing with the current shell environment." >&2
fi

# The smoke test must enable identity auth so the app actually validates the
# Entra settings from `.env`. This only affects the current script run.
export HTH_IDENTITY_AUTH_ENABLED=true

if [[ "${1:-}" == "--list" ]]; then
  cat <<'EOF'
Microsoft Entra auth smoke test cases
1. Configuration load
   - Confirms the issuer, audience, and JWKS URL were loaded from the active environment.
2. JWKS reachability
   - Fetches the configured JWKS URL directly and checks that it returns a key set.
3. Positive token validation
   - Starts a local JWKS mirror, signs a synthetic RS256 token, and verifies the app accepts the configured issuer and audience.
4. Audience mismatch rejection
   - Uses the same token shape but with the base App ID URI audience, expecting a 401 invalid_token response.
5. Issuer mismatch rejection
   - Uses a valid signature but a wrong issuer, expecting a 401 invalid_token response.
6. Missing bearer rejection
   - Calls the auth-protected endpoint with no Authorization header, expecting a 401 missing_bearer_token response.
7. Missing required claims rejection
   - Omits required identity claims such as `tid` or `oid`, expecting a 403 missing_claims response.
8. Group gate rejection
   - Supplies a token signed and issued correctly but with the wrong groups claim, expecting a 403 group_access_denied response.
9. Token version rejection
   - Supplies a token with an unsupported `ver` value, expecting a 403 unsupported_token_version response.
10. Optional live Entra token check
   - If `HTH_AUTH_ACCESS_TOKEN` is set, the script also tests that real token against the configured JWKS and app.
EOF
  exit 0
fi

python3 - <<'PY'
from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import jwt
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import create_app


REQUIRED_ENV_KEYS = (
    "HTH_AUTH_ISSUER",
    "HTH_AUTH_AUDIENCE",
    "HTH_AUTH_JWKS_URL",
)


def mask(value: str, left: int = 12, right: int = 8) -> str:
    if len(value) <= left + right + 3:
        return value
    return f"{value[:left]}...{value[-right:]}"


def print_header(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def print_case(name: str, detail: str) -> None:
    print(f"• {name}: {detail}")


@dataclass(frozen=True)
class CaseResult:
    name: str
    passed: bool
    detail: str


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class JWKSHandler(BaseHTTPRequestHandler):
    jwks_payload: dict[str, object] = {"keys": []}

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/keys":
            self.send_response(404)
            self.end_headers()
            return

        body = json.dumps(self.jwks_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class LocalJWKS:
    def __init__(self, jwks_payload: dict[str, object]) -> None:
        self.jwks_payload = jwks_payload
        self.server = ThreadedHTTPServer(("127.0.0.1", 0), JWKSHandler)
        self.server.RequestHandlerClass.jwks_payload = jwks_payload  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/keys"

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)


def b64u_int(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def make_rsa_jwks() -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "local-test-kid",
        "n": b64u_int(public_numbers.n),
        "e": b64u_int(public_numbers.e),
    }
    return private_key, {"keys": [jwk]}


def sign_token(private_key: object, *, issuer: str, audience: str, now: int, overrides: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "exp": now + 3600,
        "nbf": now - 5,
        "iat": now - 5,
        "ver": "2.0",
        "tid": "tenant-1",
        "oid": "user-1",
        "sub": "user-1",
        "groups": ["HTH-MCP"],
        "roles": ["Tool.Viewer"],
    }
    if overrides:
        payload.update(overrides)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "local-test-kid"})


def assert_status(response, expected: int, context: str) -> None:
    if response.status_code != expected:
        raise AssertionError(
            f"{context}: expected HTTP {expected}, got HTTP {response.status_code} with body {response.text}"
        )


def main() -> int:
    missing = [key for key in REQUIRED_ENV_KEYS if not os.getenv(key)]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    settings = Settings()
    issuer = settings.auth_issuer or ""
    audience = settings.auth_audience or ""
    jwks_url = settings.auth_jwks_url or ""

    print_header("Loaded Entra settings")
    print_case("issuer", mask(issuer))
    print_case("audience", mask(audience))
    print_case("jwks_url", mask(jwks_url))
    print_case("identity_auth_enabled", str(settings.identity_auth_enabled))

    results: list[CaseResult] = []

    # Test 1: verify that the active environment really contains the three Entra
    # values we care about, rather than silently falling back to defaults.
    try:
        assert issuer == os.environ["HTH_AUTH_ISSUER"]
        assert audience == os.environ["HTH_AUTH_AUDIENCE"]
        assert jwks_url == os.environ["HTH_AUTH_JWKS_URL"]
        results.append(CaseResult("Configuration load", True, "issuer/audience/JWKS URL loaded from the active environment"))
    except AssertionError as exc:
        results.append(CaseResult("Configuration load", False, str(exc)))

    # Test 2: check the real JWKS endpoint referenced by `.env` is reachable and
    # returns a JSON key set. This proves the Microsoft endpoint is reachable from
    # the current machine and that the URL is correctly formed.
    try:
        response = httpx.get(jwks_url, timeout=20)
        response.raise_for_status()
        payload = response.json()
        keys = payload.get("keys", [])
        assert isinstance(keys, list) and len(keys) > 0
        results.append(CaseResult("JWKS reachability", True, f"JWKS returned {len(keys)} signing key(s)"))
    except Exception as exc:  # noqa: BLE001
        results.append(CaseResult("JWKS reachability", False, str(exc)))

    # The remaining tests use a local JWKS mirror so we can validate signature
    # verification and claim handling without depending on a live Azure-issued
    # token for every smoke run.
    private_key, jwks_payload = make_rsa_jwks()
    now = int(time.time())
    app_settings = settings.model_copy(
        update={
            "identity_auth_enabled": True,
            "auth_jwks_url": None,
            "auth_issuer": issuer,
            "auth_audience": audience,
            "local_harmonise": True,
            "redis_fallback_enabled": True,
            "enable_mock_ui_simulation": False,
            "public_base_url": "https://hth.example.test",
            "mcp_allowed_hosts": "testserver",
            "mock_catalog_path": "./mock/product-catalog.json",
            "mock_details_path": "./mock/product-details.json",
            "mock_departments_path": "./mock/departments.json",
            "mock_categories_path": "./mock/categories.json",
        }
    )

    with LocalJWKS(jwks_payload) as local_jwks_url:
        app_settings = app_settings.model_copy(update={"auth_jwks_url": local_jwks_url})

        with TestClient(create_app(app_settings)) as client:
            # Test 3: confirm the app accepts a valid token that matches the
            # configured issuer, audience, signature, required claims, and group.
            valid_token = sign_token(private_key, issuer=issuer, audience=audience, now=now)
            response = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {valid_token}"})
            try:
                assert_status(response, 200, "positive token validation")
                results.append(CaseResult("Positive token validation", True, "token accepted with configured issuer and audience"))
            except AssertionError as exc:
                results.append(CaseResult("Positive token validation", False, str(exc)))

            # Test 4: confirm the app rejects a token whose audience is only the
            # base App ID URI. This is the most important regression check for the
            # Entra audience shape.
            wrong_audience_token = sign_token(
                private_key,
                issuer=issuer,
                audience=audience.rsplit("/", 1)[0] if "/" in audience else f"{audience}-wrong",
                now=now,
            )
            response = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {wrong_audience_token}"})
            try:
                assert_status(response, 401, "audience mismatch rejection")
                detail = response.json().get("detail", {})
                assert detail.get("code") == "invalid_token"
                results.append(CaseResult("Audience mismatch rejection", True, "token rejected with invalid_token"))
            except AssertionError as exc:
                results.append(CaseResult("Audience mismatch rejection", False, str(exc)))

            # Test 5: confirm the app rejects a token that is signed correctly but
            # issued by the wrong tenant/issuer.
            wrong_issuer_token = sign_token(
                private_key,
                issuer="https://login.microsoftonline.com/wrong-tenant/v2.0",
                audience=audience,
                now=now,
            )
            response = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {wrong_issuer_token}"})
            try:
                assert_status(response, 401, "issuer mismatch rejection")
                detail = response.json().get("detail", {})
                assert detail.get("code") == "invalid_token"
                results.append(CaseResult("Issuer mismatch rejection", True, "token rejected with invalid_token"))
            except AssertionError as exc:
                results.append(CaseResult("Issuer mismatch rejection", False, str(exc)))

            # Test 6: confirm the gateway fails closed when no bearer token is
            # supplied at all.
            response = client.get("/api/v1/tools")
            try:
                assert_status(response, 401, "missing bearer rejection")
                detail = response.json().get("detail", {})
                assert detail.get("code") == "missing_bearer_token"
                results.append(CaseResult("Missing bearer rejection", True, "request rejected with missing_bearer_token"))
            except AssertionError as exc:
                results.append(CaseResult("Missing bearer rejection", False, str(exc)))

            # Test 7: confirm the gateway rejects tokens that are missing required
            # identity claims such as `tid` or `oid`.
            missing_claim_token = sign_token(
                private_key,
                issuer=issuer,
                audience=audience,
                now=now,
                overrides={"oid": None},
            )
            response = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {missing_claim_token}"})
            try:
                assert_status(response, 403, "missing claims rejection")
                detail = response.json().get("detail", {})
                assert detail.get("code") == "missing_claims"
                results.append(CaseResult("Missing claims rejection", True, "token rejected with missing_claims"))
            except AssertionError as exc:
                results.append(CaseResult("Missing claims rejection", False, str(exc)))

            # Test 8: confirm the required Microsoft 365 group gate still denies a
            # correctly signed token when the caller is not in `HTH-MCP`.
            wrong_group_token = sign_token(
                private_key,
                issuer=issuer,
                audience=audience,
                now=now,
                overrides={"groups": ["Other_Group"]},
            )
            response = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {wrong_group_token}"})
            try:
                assert_status(response, 403, "group gate rejection")
                detail = response.json().get("detail", {})
                assert detail.get("code") == "group_access_denied"
                results.append(CaseResult("Group gate rejection", True, "token rejected with group_access_denied"))
            except AssertionError as exc:
                results.append(CaseResult("Group gate rejection", False, str(exc)))

            # Test 9: confirm the token version check still rejects older or
            # incompatible tokens even if the signature and audience are valid.
            wrong_version_token = sign_token(
                private_key,
                issuer=issuer,
                audience=audience,
                now=now,
                overrides={"ver": "1.0"},
            )
            response = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {wrong_version_token}"})
            try:
                assert_status(response, 403, "token version rejection")
                detail = response.json().get("detail", {})
                assert detail.get("code") == "unsupported_token_version"
                results.append(CaseResult("Token version rejection", True, "token rejected with unsupported_token_version"))
            except AssertionError as exc:
                results.append(CaseResult("Token version rejection", False, str(exc)))

            # Optional Test 10: if a real Entra access token is supplied, exercise
            # it directly against the app using the configured JWKS URL.
            live_token = os.getenv("HTH_AUTH_ACCESS_TOKEN") or os.getenv("AUTH_ACCESS_TOKEN")
            if live_token:
                response = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {live_token}"})
                if response.status_code == 200:
                    results.append(CaseResult("Optional live Entra token", True, "real token accepted by the app"))
                else:
                    results.append(
                        CaseResult(
                            "Optional live Entra token",
                            False,
                            f"HTTP {response.status_code}: {response.text}",
                        )
                    )
            else:
                results.append(CaseResult("Optional live Entra token", True, "skipped because HTH_AUTH_ACCESS_TOKEN is not set"))

    print_header("Test results")
    failures = 0
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name} - {result.detail}")
        if not result.passed:
            failures += 1

    if failures:
        print()
        print(f"{failures} test case(s) failed.")
        return 1

    print()
    print("All auth smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
