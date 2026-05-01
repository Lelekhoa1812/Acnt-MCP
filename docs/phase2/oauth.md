# Phase 2: MCP OAuth for Connector Registration

This project exposes an OAuth-shaped bridge so connector UIs can register a client, obtain a client ID and secret, and complete the MCP authorization code flow.

The server is not pretending to be a full identity provider for user sign-in. It is doing three narrower jobs:

1. Advertising the MCP discovery metadata that connector clients expect.
2. Issuing and recovering client registration records so the connector can be configured with a client ID and secret.
3. When identity auth is enabled, routing the browser through Entra login so the bridge token is bound to a real user object ID and that user's Graph group memberships.

## 1. What Is Implemented

The current OAuth surface includes:

- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`
- `POST /oauth/register`
- `GET /oauth/register/{client_id}`
- `GET /oauth/authorize`
- `POST /oauth/token`

Aliases are also available for convenience:

- `/register`
- `/authorize`
- `/token`

## 2. Discovery Metadata

The protected-resource metadata tells a client where the MCP server lives.

The authorization-server metadata tells a client how to register and how to request tokens.

Important fields:

| Field | Meaning |
| --- | --- |
| `authorization_endpoint` | Where the client sends the browser for the bridge code step (`/oauth/authorize`) |
| `token_endpoint` | Where the client exchanges the code for a token (`/oauth/token`) |
| `registration_endpoint` | Where the client creates a registration record |
| `client_id_metadata_document_supported` | `false` in this project, so connector UIs should use the manual/DCR path instead of CIMD |
| `token_endpoint_auth_methods_supported` | The bridge supports `none` and `client_secret_post` |
| `grant_types_supported` | `authorization_code` and `client_credentials` |

The project intentionally uses the manual registration path because that is the one that gives you a concrete client ID and secret you can paste into a connector UI.

## 3. Registration Flow

The most important endpoint is `POST /oauth/register`.

It creates a durable client registration and returns:

- `client_id`
- `client_secret`
- `registration_client_uri`
- `registration_access_token`
- `token_endpoint_auth_method`
- `grant_types`
- `response_types`
- `scope`

That response is the source of truth for the connector setup.

Example:

```bash
curl -X POST https://<domain>/oauth/register \
  -H 'Content-Type: application/json' \
  -d '{
    "client_name": "ChatGPT Custom Tool",
    "redirect_uris": ["https://chatgpt.com/connector/oauth/callback"]
  }'
```

Example response shape:

```json
{
  "client_id": "hth-abc123",
  "client_secret": "secret-value",
  "registration_client_uri": "https://<domain>/oauth/register/hth-abc123",
  "registration_access_token": "registration-token",
  "token_endpoint_auth_method": "client_secret_post",
  "grant_types": ["authorization_code", "client_credentials"],
  "response_types": ["code"],
  "scope": "mcp"
}
```

## 4. How The Callback Works

The callback is owned by the connector client, not by this server.

In the screenshot, the callback URL shown by ChatGPT is the redirect URI the client is asking the authorization server to use. Our `/oauth/authorize` route simply redirects the browser back to that URI with a temporary authorization code.

So:

- The connector owns the callback page.
- This server only redirects to the `redirect_uri` that the connector provides.
- You do not need to create a `/auth/callback` route on this server for the connector flow to work.
- The optional `/oauth/callback` route in this project is a separate direct Entra login helper and is not the MCP connector token endpoint.

If you are building your own browser app, then your browser app should host its own callback route and exchange the authorization code there.

## 5. Authorization Code Exchange

The authorization flow is:

1. The client calls `GET /oauth/authorize`.
2. The server validates `client_id`, `redirect_uri`, and the response type.
3. The server redirects to the provided callback with a short-lived `code`.
4. The client calls `POST /oauth/token`.
5. The server validates the code and returns the access token.

For this project, the returned access token is a bridge JWT for `/mcp`.

The bridge issues a JWT access token signed by `HTH_MCP_OAUTH_JWT_SECRET` when it is set, or by `HTH_MCP_BEARER_TOKEN` as a compatibility fallback. When `HTH_IDENTITY_AUTH_ENABLED=true`, the authorization code must be bound to a signed-in Entra user with `tid` and `oid`, and `/oauth/token` verifies that the user's delegated Graph memberships include `OAUTH_USER_GROUP` before returning the bridge JWT.

The bridge JWT includes the normalized authorization data used by `/mcp`:

- `groups`: Entra group object IDs for the signed-in user.
- `group_names`: Entra group display names for the signed-in user.
- `plugin_permissions`: plugin keys allowed by the `*_PL_GROUP` settings.

## 6. Client Credentials Flow

The token endpoint also accepts `grant_type=client_credentials`.

That flow is useful only when identity auth is disabled and the caller already has a registered client ID and secret.

Recommended token endpoint auth method:

- `client_secret_post`

If `HTH_IDENTITY_AUTH_ENABLED=true`, client credentials are denied because MCP tools are user-scoped.

If a connector UI shows `none` as the default, change it to the method returned by `/oauth/register` unless you intentionally want a public client in a non-identity deployment.

## 7. How To Find The Client ID And Secret

This is the step-by-step recovery path after implementation:

1. Open the authorization server metadata at `/.well-known/oauth-authorization-server`.
2. Register the connector client with `POST /oauth/register`.
3. Copy the response fields `client_id`, `client_secret`, `registration_client_uri`, and `registration_access_token`.
4. Paste `client_id` and `client_secret` into the connector UI.
5. Save `registration_client_uri` and `registration_access_token` somewhere safe.
6. If you ever lose the client ID or secret, call `GET /oauth/register/{client_id}` with `Authorization: Bearer <registration_access_token>`.
7. Read the returned JSON and copy the `client_id` and `client_secret` again.

That `GET` endpoint works because client registrations are persisted in the shared key-value store, not only in memory.

## 8. Trusted Auto-Registration

The project can auto-register trusted browser-based connector hosts.

Use `AUTO_TRUSTED_DOMAINS` to allow known domains:

```bash
AUTO_TRUSTED_DOMAINS=chatgpt.com,claude.ai,claude.com
```

When a client from one of those domains calls `/oauth/authorize`, the server can create a registration record automatically instead of forcing a manual pre-register step.

## 9. How This Fits With Auth

The OAuth bridge and the identity layer solve different problems.

- The OAuth bridge helps a connector get a valid client registration and a bearer token.
- The identity layer validates the actual caller identity when `HTH_IDENTITY_AUTH_ENABLED=true`.

The optional `OAUTH_*` settings configure the Microsoft Entra login helper endpoints (`/oauth/login`, `/oauth/callback`, `/oauth/token/validate`) and delegated Microsoft Graph membership lookup.

For public bootstrap without user identity, the bridge can run with only `HTH_MCP_BEARER_TOKEN` plus `HTH_MCP_OAUTH_JWT_SECRET`.

For user-scoped security (`HTH_IDENTITY_AUTH_ENABLED=true`), configure `OAUTH_CLIENT_ID`, `OAUTH_TENANT_ID`, optional `OAUTH_CLIENT_SECRET`, and delegated `OAUTH_GRAPH_SCOPES` so the bridge can authenticate the user and compare the user's own group memberships against `OAUTH_USER_GROUP` and each `*_PL_GROUP`.

Do not paste the server-side Entra `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` into GPT, Claude, or Cursor as MCP connector credentials. Connector clients should use the bridge `client_id` and `client_secret` returned by `POST /oauth/register`.

If you are using Entra JWT enforcement, follow `auth.md` for the resource-server validation rules.

If you are configuring a connector UI and need the client ID and secret, follow this file.

## 10. Troubleshooting

If the connector cannot start OAuth:

1. Confirm `HTH_MCP_BEARER_TOKEN` is set.
2. Confirm the public MCP URL is HTTPS.
3. Confirm `/.well-known/oauth-protected-resource` returns the MCP resource URL.
4. Confirm `/.well-known/oauth-authorization-server` returns `/oauth/authorize` and `/oauth/token` as the authorization and token endpoints.
5. Confirm the registered `redirect_uri` matches the connector callback exactly.
6. Confirm the connector is using the `client_id` and `client_secret` returned by `/oauth/register`.

If the connector sees `invalid_client`:

1. Re-check the `client_id` and `client_secret`.
2. Re-register the client if the record was deleted or expired.
3. Rebuild the connector configuration after copying the fresh registration response.

If the connector sees `invalid_grant`:

1. Re-check the `redirect_uri`.
2. Re-check that the authorization code is being exchanged only once.
3. Re-run the authorize step and let the client follow the redirect again.

If Cursor reports `missing_bearer_token`:

1. Confirm the connector completed `/oauth/token`.
2. Confirm the token response included `"token_type": "Bearer"`.
3. Confirm the client sends `Authorization: Bearer <access_token>` to `/mcp/`.
4. Confirm the MCP URL includes the configured path, typically `/mcp/`.

If ChatGPT reports `refresh_actions 424 Failed Dependency`:

1. Confirm discovery works from the public internet.
2. Confirm CORS allows `https://chatgpt.com`.
3. Confirm `/oauth/token` is not returning `group_access_denied`.
4. Confirm application logs do not show `Microsoft Graph delegated user membership lookup failed`.
5. Confirm the signed-in user's Graph memberships include `OAUTH_USER_GROUP` by object ID or display name.

If Claude reports authorization failure:

1. Re-register the connector or retrieve the saved registration with `registration_client_uri`.
2. Confirm the redirect URI exactly matches the Claude callback.
3. Confirm Entra login reaches `/oauth/callback` and redirects back to Claude with a bridge code.
4. Confirm `/oauth/token` returns a bridge JWT rather than `group_access_denied` or a delegated Graph lookup error.

## 11. Practical Setup Checklist

1. Set `HTH_MCP_BEARER_TOKEN`.
2. Prefer setting `HTH_MCP_OAUTH_JWT_SECRET` so the bridge has a dedicated signing key.
3. Set `HTH_PUBLIC_BASE_URL` to the public HTTPS origin.
4. Set `AUTO_TRUSTED_DOMAINS` if you want trusted browser connectors to auto-register.
5. Allow `https://chatgpt.com`, `https://claude.ai`, and `https://claude.com` in `HTH_MCP_ALLOWED_ORIGINS` when hosted browser clients need CORS access to discovery or OAuth responses.
6. If identity auth is enabled, set the Entra `OAUTH_*`, `HTH_AUTH_*`, `OAUTH_GRAPH_SCOPES`, and delegated Graph permission settings described in `auth.md`.
7. Register the client with `POST /oauth/register`.
8. Paste the returned `client_id` and `client_secret` into the connector UI.
9. Save `registration_client_uri` and `registration_access_token`.
10. Test one authorize redirect, one token exchange, and one `/mcp/` initialize call with `Authorization: Bearer <access_token>`.

## 12. Connector Handoff Examples

Cursor PKCE exchange:

```bash
curl -sS -D - \
  'https://<domain>/oauth/authorize?response_type=code&client_id=<client_id>&redirect_uri=https%3A%2F%2Fcursor.sh%2Foauth%2Fcallback&code_challenge=<verifier>&code_challenge_method=plain'

curl -sS -X POST 'https://<domain>/oauth/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode 'client_id=<client_id>' \
  --data-urlencode 'code=<code>' \
  --data-urlencode 'redirect_uri=https://cursor.sh/oauth/callback' \
  --data-urlencode 'code_verifier=<verifier>'
```

ChatGPT callback:

```bash
curl -sS -X POST 'https://<domain>/oauth/register' \
  -H 'Content-Type: application/json' \
  -d '{"client_name":"ChatGPT","redirect_uris":["https://chatgpt.com/connector/oauth/callback"]}'
```

Claude trusted redirect:

```bash
curl -sS -D - \
  'https://<domain>/oauth/authorize?response_type=code&client_id=<client_id>&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth%2Fcallback'
```

MCP bearer handoff:

```bash
curl -sS -X POST 'https://<domain>/mcp/' \
  -H 'accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer <access_token>' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"debug","version":"1.0.0"}}}'
```
