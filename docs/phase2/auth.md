# Phase 2: Authentication and Identity Enforcement

This project uses two different security layers:

1. `auth.md` covers the resource-server identity layer that validates who is allowed to call the MCP and REST surfaces.
2. `oauth.md` covers the MCP OAuth bridge and client registration flow used by connector UIs.

The important distinction is:

- Authentication answers, "Is this token real?"
- Authorization answers, "Is this identity allowed to use this server or tool?"
- OAuth registration answers, "How does a connector obtain the client credentials needed to start the flow?"

## 1. What This Layer Does

The server does not implement a human sign-in page. It validates bearer tokens on inbound requests and then decides whether the request may proceed.

When the token comes from the MCP OAuth bridge, it is a server-signed JWT rather than a raw opaque string. The bridge token is still validated by the same `IdentityGateway`.

The identity checks apply to:

- `POST /api/v1/tools`
- `POST /api/v1/tools/call`
- `POST /mcp`
- Any other route that opts into the shared `IdentityGateway`

The current implementation is intentionally fail-closed:

- Missing or malformed bearer token -> `401`
- Invalid signature, issuer, audience, or expiry -> `401`
- Missing required claims -> `403`
- Wrong tenant/group/role -> `403`
- Per-user rate limit exceeded -> `429`

## 2. Entra Configuration

For production identity enforcement, configure Microsoft Entra ID as the token issuer.

Use two app registrations:

1. Resource app
2. Client app

Resource app settings:

| Setting | Purpose | Notes |
| --- | --- | --- |
| `requestedAccessTokenVersion` | Use v2 access tokens | Keep this at `2` |
| App ID URI | Audience for access tokens | Example: `api://<resource-app-id>` |
| Scope | Permission that the client requests | Example: `MCP.Invoke` |
| App roles | Tool or admin roles | Example: `Tool.Viewer`, `Tool.Admin` |

Client app settings:

| Setting | Purpose | Notes |
| --- | --- | --- |
| Redirect URI | Where the client receives the code | This is owned by the client, not this server |
| Delegated permission | What the client can ask for | Usually the resource scope plus any standard OpenID scopes the client needs |
| Pre-authorization | Avoid repeat consent prompts | Useful for trusted internal clients |

## 3. Environment Variables

These settings control the identity gateway:

| Variable | Meaning | Recommended value |
| --- | --- | --- |
| `HTH_IDENTITY_AUTH_ENABLED` | Turns JWT validation on | `true` in production |
| `HTH_AUTH_ISSUER` | Token issuer URL | `https://login.microsoftonline.com/<tenant-id>/v2.0` |
| `HTH_AUTH_AUDIENCE` | Expected token audience | `api://<resource-app-id>` |
| `HTH_AUTH_JWKS_URL` | Public key endpoint for signature validation | Entra JWKS URL for the tenant |
| `HTH_AUTH_JWT_HS256_SECRET` | Local-only symmetric test secret | Development only |
| `HTH_MCP_OAUTH_JWT_SECRET` | Signing key for bridge-issued access tokens | Prefer a dedicated secret in production |
| `HTH_AUTH_REQUIRED_GROUP` | Group gate | `HTH-MCP` by default |
| `HTH_AUTH_REQUIRED_CLAIMS` | Minimum claims required in the token | `tid,oid` by default |
| `HTH_AUTH_REQUIRED_TOKEN_VERSION` | Access token version check | `2.0` |
| `HTH_AUTH_RATE_LIMIT_PER_MINUTE` | Per-user tool-call throttle | `50` by default |

`HTH_AUTH_JWT_HS256_SECRET` is only for local test tokens. Do not use it in production.
`HTH_MCP_OAUTH_JWT_SECRET` signs bridge-issued access tokens; if it is not set, the bridge falls back to `HTH_MCP_BEARER_TOKEN` for compatibility.

## 4. Validation Rules

The `IdentityGateway` performs the checks in this order:

1. Extract the `Authorization` header.
2. Decode the token using HS256 or the Entra JWKS.
3. Verify issuer, audience, and expiry.
4. Verify required claims such as `tid` and `oid`.
5. Verify token version when `HTH_AUTH_REQUIRED_TOKEN_VERSION` is set.
6. Verify the user belongs to the required group.
7. Apply the per-user rate limit.

The server stores the claims in `UserContext`, which carries:

- `tenant_id`
- `user_id`
- `subject`
- `roles`
- `groups`
- raw `claims`

## 5. Authorization Model

The project uses a combination of RBAC and lightweight ABAC.

RBAC:

- `Tool.Viewer` grants access to normal tools.
- `Tool.Admin` is reserved for admin-only workflows.

ABAC:

- Tool access can be filtered based on claims.
- `tools/list` is filtered before a client sees the catalog.
- `tools/call` is checked again so a forged request cannot bypass discovery filtering.

The shared tool registry applies role gates centrally so the REST surface and MCP surface behave the same way.

## 6. Tool Visibility

Tool discovery is not static.

If a user lacks the required role for a tool:

- the tool is hidden from `list_tools`
- the tool is rejected again if a client tries to invoke it directly

That keeps the surface least-privileged even when a client already knows a tool name.

## 7. Failure Modes

The main auth error cases are:

| Error | HTTP status | Meaning |
| --- | --- | --- |
| `missing_bearer_token` | `401` | No bearer token was provided |
| `invalid_bearer_token` | `401` | Token header was empty or malformed |
| `invalid_token` | `401` | JWT failed verification |
| `missing_claims` | `403` | Required claims were absent |
| `unsupported_token_version` | `403` | Token version did not match the configured version |
| `group_access_denied` | `403` | User is not in the required group |
| `tool_access_denied` | `403` | Token is valid, but the requested tool requires a role the user does not have |
| `rate_limited` | `429` | The user exceeded the per-minute call budget |

For MCP callers, auth failures are returned in a structured JSON error body and include the `mcp_error_code` used by the transport layer.

## 8. Local Testing

For local tests, you can validate the auth path with a signed HS256 JWT.

Suggested development setup:

```bash
HTH_IDENTITY_AUTH_ENABLED=true
HTH_AUTH_JWT_HS256_SECRET=test-secret
HTH_AUTH_ISSUER=https://login.microsoftonline.com/test-tenant/v2.0
HTH_AUTH_AUDIENCE=api://hth-mcp
HTH_AUTH_REQUIRED_GROUP=HTH-MCP
```

Example claims for a test token:

```json
{
  "iss": "https://login.microsoftonline.com/test-tenant/v2.0",
  "aud": "api://hth-mcp",
  "ver": "2.0",
  "tid": "tenant-1",
  "oid": "user-1",
  "sub": "user-1",
  "roles": ["Tool.Viewer"],
  "groups": ["HTH-MCP"]
}
```

## 9. Production Checklist

1. Set `HTH_IDENTITY_AUTH_ENABLED=true`.
2. Set the Entra `issuer`, `audience`, and `jwks` values.
3. Set `HTH_AUTH_REQUIRED_GROUP` to the real access group.
4. Confirm the token contains `tid` and `oid`.
5. Confirm the token has `ver: 2.0`.
6. Assign `Tool.Admin` only to the few operators who need it.
7. Keep `HTH_AUTH_JWT_HS256_SECRET` out of production.
8. Test one normal tool call and one denied call before rollout.

## 10. What This Layer Is Not

This layer is not the OAuth client-registration flow shown in the connector UI.

If you are configuring a connector and need the OAuth client ID and secret, read `oauth.md`. That file explains the registration endpoint, callback handling, and how to recover the client credentials later.
