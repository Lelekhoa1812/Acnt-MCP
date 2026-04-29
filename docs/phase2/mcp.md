# Phase 2: Azure Deployment for Claude.ai Browser Access

The current FastAPI app run `uvicorn app.main:app` exposes diagnostics plus `StreamableHTTPSessionManager` at `/mcp`, and the Claude connector calls `tools/list`/`tools/call` over HTTPS/SSE.

## References
- [Anthropic MCP overview](https://docs.anthropic.com/en/docs/mcp)
- [Anthropic MCP connector](https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector)
- [Anthropic Claude Code MCP docs](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [Azure Container Apps ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview)
- [Azure Container Apps environment variables](https://learn.microsoft.com/en-us/azure/container-apps/environment-variables)

-## Summary
- **Compute tier:** Host `uvicorn app.main:app` in an Azure Container App (standard SKU) or Linux App Service with HTTPS ingress on port 80 and the `/mcp` path so the StreamableHTTPSessionManager is public. Container Apps is preferred because it handles TLS, custom domains, and revision control. Target at least a **Standard D2s v4** Container App (2 vCPU/4 GB RAM) or a Linux App Service plan with the equivalent D2 pricing tier so tools/list/call latency stays under a second during bursts; scale to **Standard D4s v4** (4 vCPU/8 GB RAM) or add autoscale rules (5–10 replica cap, based on CPU > 70% for >5 min) if you hit >2 requests per second from Claude.
- **Redis sidecar:** Run Azure Cache for Redis (or a managed sidecar) in the same environment and inject `HTH_REDIS_URL` via app settings; keep the connection secret in Key Vault for the container to consume securely.
- **Azure configuration:** Surface `HTH_PUBLIC_BASE_URL`, `HTH_MCP_BEARER_TOKEN`, Harmonise credentials (`CLOUD_HARMONISE_*`), `HTH_MCP_ALLOWED_HOSTS`/`ORIGINS`, and session tuning flags (`HTH_MCP_STATELESS`, `HTH_MCP_SESSION_IDLE_TIMEOUT_SECONDS`, `HTH_MCP_RETRY_INTERVAL_MS`) as environment variables, preferably referencing Key Vault secrets with `@Microsoft.KeyVault(...)`. Require TLS plus bearer-token or OAuth auth so Claude hits `https://<domain>/mcp` over HTTP/SSE. A bearer token automatically keeps the OAuth bridge available for browser connectors. The auth metadata now advertises Client ID Metadata Documents, which is the preferred registration path for many web MCP clients. Set `AUTO_TRUSTED_DOMAINS=chatgpt.com,claude.ai,claude.com` when you want to auto-accept Claude/ChatGPT connectors without manually posting to `/oauth/register`.
- **Claude.ai configuration:** Add a remote MCP connector, point it at your `/mcp` URL, refresh the tool list, and confirm `tools/list`/`tools/call` go through the shared orchestrator. Claude Desktop/API clients can still use `Authorization: Bearer <HTH_MCP_BEARER_TOKEN>` directly.

## Deploy checklist
- Build/push the Docker image with `script/push.sh` (`REGISTRY=youracr.azurecr.io ./script/push.sh`) so ACR hosts `hth-mcp/hth-harmonise-mcp:<date>-<version>` and Container Apps pulls that image.
- Create the Container App environment (`az containerapp env create`), then `az containerapp create` with external HTTPS ingress, target port 80, and app settings for `LOCAL_HARMONISE=false`, `CLOUD_HARMONISE_*`, `HTH_REDIS_URL`, `HTH_LOG_LEVEL`, `HTH_PUBLIC_BASE_URL=https://<domain>`, `HTH_MCP_PATH=/mcp`, `HTH_MCP_BEARER_TOKEN=@Microsoft.KeyVault(...)`, and any other required toggles.
- Inject Redis, bearer token, and other secrets via Key Vault references so the app settings stay secure.
- Optionally use App Service (custom Linux container, HTTPS-only, deployment slots) if you prefer a classic web app model, but keep ingress/port alignment consistent.
- Disable mock/local memory toggles (`HTH_REDIS_FALLBACK_ENABLED=false`, `HTH_LOCAL_CHAT_MEMORY_ENABLED=false`, `HTH_ENABLE_MOCK_UI_SIMULATION=false`) unless you intentionally need them.
- Validate connectivity by curling `/health`, confirming `https://<domain>/mcp` does not redirect to `http://`, hitting `/mcp` with the bearer token (the stream should emit SSE `retry` hints), checking `/.well-known/oauth-protected-resource`, running `tools/list`/`tools/call`, and monitoring Redis/Harmonise responses via Application Insights or Log Analytics.

## Troubleshooting
- Claude.ai cannot see tools: ensure the endpoint is HTTPS/SSE, `/mcp` path matches `HTH_MCP_PATH`, auth token matches `HTH_MCP_BEARER_TOKEN`, `HTH_PUBLIC_BASE_URL` is the public `https://` origin, and `tools/list` is reachable. The app auto-exposes the OAuth bridge whenever a bearer token is present, so missing auth endpoints usually point to a deployment drift or an Azure app setting override. If Claude still fails to start OAuth, re-create the connector after the auth server advertises `client_id_metadata_document_supported=true`.
- Tools appear but calls fail: inspect Azure logs, check Redis connectivity, verify Harmonise data/keys, and confirm host/origin filters allow the request.
- Empty or partial answers: make sure plugin API keys exist, Harmonise returns the expected fields, and catalog timeouts/retries are tuned.

## NOTE:
- Use **MCP Inspector** for connection and tool-listing test:
```bash
 npx @modelcontextprotocol/inspector https://app-hth-mcp-dev-ause-01.azurewebsites.net/mcp
```  
- Tool names must be `[a-zA-Z0-9_-]{1,64}$` matching `claude.ai`'s rulesets for tool definitions.


## Connection steps
1. In Claude.ai workspace/team settings, add a new remote MCP connector.
2. Enter the public `https://<domain>/mcp` URL and complete the OAuth flow tied to `HTH_MCP_BEARER_TOKEN`.
3. Save, refresh the tool list, and run a safe stock lookup to confirm `tools/list`/`tools/call` are functional.

## Claude.ai tool-choice checks
- Supported scope/counts: ask “how many department and category of stock do we have?” and confirm Claude uses `stock_scope`, not raw Harmonise metadata.
- Product-family availability: ask “let me know about our Alto chair stock availability” and confirm Claude uses `stock_snapshot` and summarizes every returned variant/SKU.
- Grouped regional totals: ask “which type of chair has the most stock in NSW production-wise?” and confirm Claude uses `stock_aggregate` with prompt-supplied `search`, `region="NSW"`, `measure="stock"`, `groupBy="product"`, and `direction="most"`. It should not use variant ranking for this grain.
- Regional variant ranking: ask “which Charlie chair variant is most in stock in Victoria?” and confirm Claude uses `stock_variant_rank` with `metric="stock"` and `region="VIC"`.
- Exact variant detail: use `stock_detail` after a SKU or product ID is known.
- Fallback planning: if the first stock search returns no rows, partial coverage, or timeouts, Claude should retry with a shorter distinctive phrase or broader `stock_scope` filter before reporting the limitation. Grouped aggregation now paginates automatically in the backend, so the model should not try to tune `pageSize`.
- Avoid hidden/admin tools in normal Claude.ai discovery: deprecated aliases, raw local metadata, and session clearing are kept callable for compatibility but should not appear as primary choices.

Running `python3 -m app.mcp.server` remains useful for local CLI clients, but browser-based Claude access must go through the `/mcp` transport hosted on Azure.
