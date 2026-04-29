# Phase 2: MCP OAuth for Connector Registration

This project exposes an OAuth-shaped bridge so connector UIs can register a client, obtain a client ID and secret, and complete the MCP authorization code flow.

The server is not pretending to be a full identity provider for user sign-in. It is doing two narrower jobs:

1. Advertising the MCP discovery metadata that connector clients expect.
2. Issuing and recovering client registration records so the connector can be configured with a client ID and secret.

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
| `authorization_endpoint` | Where the client sends the browser for the code step |
| `token_endpoint` | Where the client exchanges the code for a token |
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

If you are building your own browser app, then your browser app should host its own callback route and exchange the authorization code there.

## 5. Authorization Code Exchange

The authorization flow is:

1. The client calls `GET /oauth/authorize`.
2. The server validates `client_id`, `redirect_uri`, and the response type.
3. The server redirects to the provided callback with a short-lived `code`.
4. The client calls `POST /oauth/token`.
5. The server validates the code and returns the access token.

For this project, the returned access token is the MCP bearer token configured in `HTH_MCP_BEARER_TOKEN`.

That means the OAuth bridge is a connector bootstrap mechanism, not a separate identity provider.
The bridge issues a JWT access token signed by `HTH_MCP_OAUTH_JWT_SECRET` when it is set, or by `HTH_MCP_BEARER_TOKEN` as a compatibility fallback.

## 6. Client Credentials Flow

The token endpoint also accepts `grant_type=client_credentials`.

That flow is useful when the caller already has a registered client ID and secret and wants to fetch the configured bearer token directly.

Recommended token endpoint auth method:

- `client_secret_post`

If a connector UI shows `none` as the default, change it to the method returned by `/oauth/register` unless you intentionally want a public client.

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

If you are using Entra JWT enforcement, follow `auth.md` for the resource-server validation rules.

If you are configuring a connector UI and need the client ID and secret, follow this file.

## 10. Troubleshooting

If the connector cannot start OAuth:

1. Confirm `HTH_MCP_BEARER_TOKEN` is set.
2. Confirm the public MCP URL is HTTPS.
3. Confirm `/.well-known/oauth-protected-resource` returns the MCP resource URL.
4. Confirm `/.well-known/oauth-authorization-server` returns the registration and token endpoints.
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

## 11. Practical Setup Checklist

1. Set `HTH_MCP_BEARER_TOKEN`.
2. Prefer setting `HTH_MCP_OAUTH_JWT_SECRET` so the bridge has a dedicated signing key.
3. Set `HTH_PUBLIC_BASE_URL` to the public HTTPS origin.
4. Set `AUTO_TRUSTED_DOMAINS` if you want trusted browser connectors to auto-register.
5. Register the client with `POST /oauth/register`.
6. Paste the returned `client_id` and `client_secret` into the connector UI.
7. Save `registration_client_uri` and `registration_access_token`.
8. Test one authorize redirect and one token exchange.
