# Phase 2: Identity Authorization

This service protects MCP tools with Microsoft Entra JWT validation plus group membership authorization.

The intended user allow-list is `SG-HTH-MCP-Users`. The app resolves that display name to its Microsoft Graph group object ID, loads the group's direct member object IDs, and compares the signed-in user's `oid` claim against that set.

## Required App Settings

| Setting | Purpose |
| --- | --- |
| `HTH_IDENTITY_AUTH_ENABLED=true` | Enables JWT and group enforcement for REST and `/mcp` traffic. |
| `HTH_AUTH_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0` | Required issuer for accepted Entra access tokens. |
| `HTH_AUTH_AUDIENCE=<api-audience>` | Required audience for accepted tokens and bridge JWTs. |
| `HTH_AUTH_JWKS_URL=https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys` | Entra signing keys. |
| `HTH_AUTH_REQUIRED_GROUP=SG-HTH-MCP-Users` | Display name or object ID of the allowed group. |
| `HTH_AUTH_REQUIRED_CLAIMS=tid,oid` | Requires tenant and user object ID. |
| `HTH_AUTH_REQUIRED_TOKEN_VERSION=2.0` | Rejects unexpected token versions. |
| `HTH_AUTH_GROUP_CACHE_TTL_SECONDS=300` | Caches resolved group IDs and member object IDs briefly. |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_TENANT_ID` | Used for Microsoft Graph app-only group lookup and Entra browser login. |

Use Key Vault references or platform secrets for all secrets. Do not commit bearer tokens, client secrets, Redis URLs with credentials, or API keys.

## Microsoft Graph Permissions

The Entra app used by `OAUTH_CLIENT_ID` must have admin-consented application permission to read group membership.

Recommended permission:

- `GroupMember.Read.All`

Equivalent broader permissions can work, but use least privilege where possible. Without this permission, users may complete Microsoft login but fail connector token exchange or `/mcp` authorization with a group lookup error.

## Authorization Flow

1. The connector starts the MCP OAuth bridge at `/oauth/authorize`.
2. When `OAUTH_*` Entra login settings are present, the bridge sends the browser through `/oauth/login` and `/oauth/callback`.
3. The callback validates the Entra access token signature, issuer, audience, expiry, `tid`, and `oid`.
4. `/oauth/token` validates that the signed-in `oid` is a direct member of `SG-HTH-MCP-Users` before issuing the MCP bridge JWT.
5. Every `/mcp` request validates the bearer token again and repeats authorization. Cached Graph results avoid repeated network calls during the short TTL.

Client credentials are denied when identity auth is enabled because MCP tools are user-scoped.

## Troubleshooting

### Cursor: `missing_bearer_token`

Cursor reached `/mcp` without `Authorization: Bearer <access_token>`.

Check:

1. The connector is configured for OAuth, not unauthenticated HTTP.
2. `/.well-known/oauth-protected-resource` advertises the correct HTTPS `/mcp` resource.
3. `/.well-known/oauth-authorization-server` advertises `/oauth/authorize` and `/oauth/token`.
4. The `/oauth/token` response contains `token_type: Bearer`.
5. Cursor is sending that returned access token to `/mcp/`.

### ChatGPT: `refresh_actions 424 Failed Dependency`

ChatGPT usually reports this when a dependency in discovery, OAuth exchange, or MCP initialization failed.

Check:

1. The public URL uses HTTPS and matches `HTH_PUBLIC_BASE_URL`.
2. `https://chatgpt.com` is allowed by CORS; this is automatic when `HTH_MCP_BEARER_TOKEN` enables browser OAuth.
3. The ChatGPT redirect URI exactly matches the registered `redirect_uris` value.
4. `/oauth/token` is not returning `group_access_denied`, `group_lookup_config_missing`, or `group_membership_lookup_failed`.
5. The signed-in user's Entra `oid` is a direct member of `SG-HTH-MCP-Users`.

### Claude: authorization failed

Claude's generic authorization failure usually means one of these happened:

1. The connector registration was missing or stale.
2. The callback exchanged an authorization code more than once.
3. Entra login succeeded but the user was not in `SG-HTH-MCP-Users`.
4. Microsoft Graph group lookup failed because app permissions or `OAUTH_*` settings were missing.
5. `/mcp` rejected the bridge JWT because issuer/audience/signature settings do not match.

Re-register the connector, retry login, then inspect the `/oauth/token` response body and application logs for the exact `error` code.

## Security Practices Applied

- JWTs must validate issuer, audience, signature, expiry, token version, `tid`, and `oid`.
- Group authorization uses immutable Entra object IDs, not email addresses or display names.
- `SG-HTH-MCP-Users` can be configured as a display name for operators, but it is resolved to its object ID before authorization.
- Microsoft Graph app credentials stay server-side.
- OAuth callback pages never render access tokens or refresh tokens.
- Bridge JWTs are signed with `HTH_MCP_OAUTH_JWT_SECRET` when set, otherwise the bearer token compatibility fallback is used.
- Client credentials cannot access user-scoped MCP tools when identity auth is enabled.
- Short group caches reduce Graph traffic without becoming a long-lived authorization source.
