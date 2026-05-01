# Phase 2: Identity Authorization

This service protects MCP tools with Microsoft Entra login, JWT validation, and user-centric group authorization.

The intended MCP allow-list is `OAUTH_USER_GROUP`, usually `SG-HTH-MCP-Users`. During the browser login flow, the app uses the signed-in user's delegated Microsoft Graph token to read that user's transitive group memberships from `/me/transitiveMemberOf/microsoft.graph.group`. It compares the returned group object IDs and display names against `OAUTH_USER_GROUP` and each `*_PL_GROUP`.

The app does not query configured groups by display name and does not enumerate group members.

## Required App Settings

| Setting | Purpose |
| --- | --- |
| `HTH_IDENTITY_AUTH_ENABLED=true` | Enables JWT and group enforcement for REST and `/mcp` traffic. |
| `HTH_AUTH_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0` | Required issuer for accepted Entra or bridge JWTs. |
| `HTH_AUTH_AUDIENCE=<api-audience>` | Required audience for accepted bridge JWTs. |
| `HTH_AUTH_JWKS_URL=https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys` | Entra signing keys when accepting Entra JWTs directly. |
| `OAUTH_USER_GROUP=SG-HTH-MCP-Users` | Display name or object ID of the group allowed to use the MCP. |
| `NEWS_PL_GROUP`, `WEATHER_PL_GROUP`, `CURRENCY_PL_GROUP`, `STOCK_PL_GROUP` | Display name, object ID, `all`, or empty value for each plugin. |
| `OAUTH_CLIENT_ID`, `OAUTH_TENANT_ID` | Entra app used for browser login. |
| `OAUTH_CLIENT_SECRET` | Optional secret for confidential web-client redemption. Leave unused for public/SPA app registrations. |
| `OAUTH_CLIENT_AUTH_METHOD` | Optional `none` or `client_secret_post`; defaults to `none` for PKCE/public-client compatibility. |
| `OAUTH_GRAPH_SCOPES=User.Read Group.Read.All` | Delegated Graph scopes requested at login so the app can read the signed-in user's groups. |
| `HTH_AUTH_REQUIRED_CLAIMS=tid,oid` | Requires tenant and user object ID. |
| `HTH_AUTH_REQUIRED_TOKEN_VERSION=2.0` | Rejects unexpected token versions. |

Use Key Vault references or platform secrets for all secrets. Do not commit bearer tokens, client secrets, Redis URLs with credentials, or API keys.

## Microsoft Graph Permissions

The Entra app used by `OAUTH_CLIENT_ID` must have admin-consented delegated permission to read the signed-in user's group memberships.

Recommended delegated permission:

- `Group.Read.All`

`GroupMember.Read.All` is a narrower alternative if tenant policy allows it for the required `/me/transitiveMemberOf` call. Application permissions are not required for group authorization in this architecture.

## Authorization Flow

1. The connector starts the MCP OAuth bridge at `/oauth/authorize`.
2. When `OAUTH_*` Entra login settings are present, the bridge sends the browser through `/oauth/login` and `/oauth/callback`.
3. The callback exchanges the code for Entra tokens, validates the ID token when present, and calls Microsoft Graph with the delegated access token.
4. Graph returns the signed-in user's transitive group object IDs and display names.
5. `/oauth/token` requires membership in `OAUTH_USER_GROUP`, computes plugin permissions from the `*_PL_GROUP` settings, and issues the MCP bridge JWT.
6. Every `/mcp` request validates the bridge JWT again and enforces the embedded group and plugin permissions without calling Graph.

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

ChatGPT usually reports this when discovery, OAuth exchange, Entra login, Graph membership lookup, or MCP initialization failed.

Check:

1. The public URL uses HTTPS and matches `HTH_PUBLIC_BASE_URL`.
2. `https://chatgpt.com` is allowed by CORS; this is automatic when `HTH_MCP_BEARER_TOKEN` enables browser OAuth.
3. The ChatGPT redirect URI exactly matches the registered `redirect_uris` value.
4. `/oauth/token` is not returning `group_access_denied`.
5. Application logs do not show `Microsoft Graph delegated user membership lookup failed`.
6. The signed-in user's Graph memberships include `OAUTH_USER_GROUP` by object ID or display name.

### Claude: authorization failed

Claude's generic authorization failure usually means one of these happened:

1. The connector registration was missing or stale.
2. The callback exchanged an authorization code more than once.
3. Entra login succeeded but the user was not in `OAUTH_USER_GROUP`.
4. Microsoft Graph delegated membership lookup failed because delegated scopes or admin consent are missing.
5. `/mcp` rejected the bridge JWT because issuer/audience/signature settings do not match.

Re-register the connector, retry login, then inspect the `/oauth/token` response body and application logs for the exact `error` code.

## Security Practices Applied

- JWTs must validate issuer, audience, signature, expiry, token version, `tid`, and `oid`.
- Group authorization uses the signed-in user's Graph memberships, not configured group member enumeration.
- Group configs may use immutable Entra object IDs or display names; object IDs are preferred for long-term stability.
- Microsoft Graph delegated access tokens are used only during login and are not rendered in callback pages.
- OAuth callback pages never render access tokens or refresh tokens.
- Bridge JWTs embed `groups`, `group_names`, and `plugin_permissions`.
- Bridge JWTs are signed with `HTH_MCP_OAUTH_JWT_SECRET` when set, otherwise the bearer token compatibility fallback is used.
- Client credentials cannot access user-scoped MCP tools when identity auth is enabled.
