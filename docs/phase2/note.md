How to find the OAuth client ID and secret:
1. Set HTH_MCP_BEARER_TOKEN and deploy the server.
2. Call POST /oauth/register.
3. Save the response fields:
- client_id
- client_secret
- registration_client_uri
- registration_access_token
4. Paste client_id and client_secret into the connector UI.
5. If you lose them later, call GET /oauth/register/{client_id} with:
- Authorization: Bearer <registration_access_token>
6. Copy the returned client_id and client_secret again.

---

## GET ACCESS:

1. Register the client
```bash
curl -sS -X POST 'https://app-hth-mcp-dev-ause-01.azurewebsites.net/oauth/register' \
  -H 'Content-Type: application/json' \
  -d '{"client_name":"My Connector"}'
```

2. From the registration response, use:
- `client_id`
- `client_secret`
- `redirect_uris` value `https://chatgpt.com/connector/oauth/callback`

3. Start the authorization step
```bash
curl -sS -D - -o /tmp/auth.txt \
  'https://app-hth-mcp-dev-ause-01.azurewebsites.net/oauth/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fconnector%2Foauth%2Fcallback&state=pytest-state' \
  -H 'accept: text/html,application/xhtml+xml'
```

4. Extract the `code` from the redirect URL in the `Location` header

5. Exchange the code for a token
```bash
curl -sS -X POST 'https://app-hth-mcp-dev-ause-01.azurewebsites.net/oauth/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode 'code=YOUR_CODE' \
  --data-urlencode 'redirect_uri=https://chatgpt.com/connector/oauth/callback' \
  --data-urlencode 'client_id=YOUR_CLIENT_ID' \
  --data-urlencode 'client_secret=YOUR_CLIENT_SECRET'
```

6. Take the `access_token` from that JSON response and send it to `/mcp/`
```bash
curl -sS -X POST 'https://app-hth-mcp-dev-ause-01.azurewebsites.net/mcp/' \
  -H 'accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer YOUR_ACCESS_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "capabilities": {},
      "clientInfo": {"name": "pytest", "version": "1.0.0"}
    }
  }'
```