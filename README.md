# HTH Stock Intelligence

HTH Stock Intelligence now exposes two separate integration surfaces:

- a real MCP server over `stdio` for Claude, Cursor, Claude Code, and similar coding apps
- a companion FastAPI app under `/api/v1/*` for local diagnostics, query demos, and the mock UI

The MCP server reuses the existing tool registry and service container. It does not duplicate business logic or expose a fake REST shim under `/mcp`.

## Architecture

### MCP surface

The standards-based MCP runtime is launched with:

```bash
python3 -m app.mcp.server
```

It exposes only MCP `tools` in this refactor:

- `stock.*`
- `resolver.*`
- `session.*`
- `weather.*`
- `news.*`
- `currency.*`

Tool execution is backed by the existing `ToolRegistry`, so the MCP server and the REST diagnostics call the same inventory and plugin services.

### REST companion surface

The FastAPI app remains available for local testing and demo workflows:

- `GET /`
- `GET /api/v1/health`
- `GET /api/v1/system/spec`
- `GET /api/v1/tools`
- `POST /api/v1/tools/call`
- `POST /api/v1/query`
- `GET /api/v1/ui`

These routes are custom REST endpoints, not MCP.

## Session and cache persistence

Session state and shared tool caches flow through `SessionStore` → `AppKeyValueStore` → **Redis** (see `app/session/store.py` and `app/store.py`). By default `HTH_REDIS_FALLBACK_ENABLED=false`, so the process **requires** a reachable Redis at `HTH_REDIS_URL`; there is no silent in-memory substitute across restarts.

- **TTL:** `HTH_SESSION_TTL_SECONDS` (default 1800) controls how long each session key is kept in Redis. Increase it if you need longer conversational memory per `sessionId`.
- **Local chat memory (REST only):** `HTH_LOCAL_CHAT_MEMORY_ENABLED=true` stores a short recent transcript in process memory on the local device (never Redis) for `/api/v1/query` follow-up context. Window size is `HTH_LOCAL_CHAT_MEMORY_TURNS` turn pairs.
- **MCP behavior:** Claude/Cursor MCP runs already carry their own conversation thread, so set `HTH_LOCAL_CHAT_MEMORY_ENABLED=false` in MCP config if you do not need the local REST transcript buffer.
- **Health:** `GET /api/v1/health` returns `session_cache_backend` (`redis` or `memory` if fallback is on), `redis_client_connected`, `redis_fallback_enabled`, plus `local_chat_memory_enabled` and `local_chat_memory_turns`.
- **Verify Redis:** `redis-cli -u "$HTH_REDIS_URL" ping` should respond with `PONG`. On startup, logs include `key_value_store backend=redis ...` when the persistent path is active.

```mermaid
flowchart LR
  AgentQuery["AgentQueryRequest"] --> SessionStore
  SessionStore["SessionStore"] --> KVS["AppKeyValueStore"]
  KVS --> Redis["Redis"]
```

For local development **without** Redis, set `HTH_REDIS_FALLBACK_ENABLED=true` in `.env` (in-memory only; not durable across process restarts).
For local development **with REST query follow-ups**, keep `HTH_LOCAL_CHAT_MEMORY_ENABLED=true` so `/api/v1/query` can replay recent turns even when Claude browser memory is not in the loop.

## Setup

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`

Use `.env.example` as the starting point.

Inventory-only local development works with:

- `LOCAL_HARMONISE=true`
- A running Redis at `HTH_REDIS_URL` (default `redis://localhost:6379`), **or** `HTH_REDIS_FALLBACK_ENABLED=true` if you accept non-persistent in-memory cache only

`/api/v1/query` additionally requires:

- `AZURE_AI_FOUNDRY_ENDPOINT`
- `AZURE_AI_FOUNDRY_API_KEY`

External plugin tools additionally require:

- `EXCHANGE_RATE_API`
- `OPEN_WEATHER_API`
- `NEWS_API`

### 3. Run the REST app

```bash
uvicorn app.main:app --reload --port 3000
```

### 4. Run the MCP server

```bash
python3 -m app.mcp.server
```

### 5. Optional: run the Harmonise simulator directly

```bash
uvicorn harmonise.main:app --reload --port 9000
```

## MCP integration

### Project `.mcp.json`

The repo includes a working example at `.mcp.json`:

```json
{
  "mcpServers": {
    "hth-stock-intelligence": {
      "command": "python3",
      "args": ["-m", "app.mcp.server"],
      "env": {
        "LOCAL_HARMONISE": "true",
        "HTH_REDIS_FALLBACK_ENABLED": "false",
        "HTH_LOCAL_CHAT_MEMORY_ENABLED": "false",
        "HTH_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

This assumes the coding app launches the server from the repo root so `.env` is available automatically, and that Redis is running unless you override `HTH_REDIS_FALLBACK_ENABLED` to `true` for in-memory-only mode.

### Claude Desktop example

Use the same command in the Claude Desktop MCP config and replace the path with your local checkout:

```json
{
  "mcpServers": {
    "hth-stock-intelligence": {
      "command": "python3",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/absolute/path/to/hth-mcp",
      "env": {
        "LOCAL_HARMONISE": "true",
        "HTH_REDIS_FALLBACK_ENABLED": "false",
        "HTH_LOCAL_CHAT_MEMORY_ENABLED": "false",
        "HTH_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

## REST diagnostics

### List tools

```bash
curl http://localhost:3000/api/v1/tools
```

### Call a tool

```bash
curl -X POST http://localhost:3000/api/v1/tools/call \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "stock.search_catalogue",
    "args": {
      "page": 1,
      "pageSize": 5,
      "search": "white gloss dance floor"
    }
  }'
```

### Run the query demo

```bash
curl -X POST http://localhost:3000/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Compare fl-la-la-lam-1-ble vs fl-da-dan",
    "renderMockUi": true,
    "includeThoughts": true
  }'
```

## Mock UI

When `HTH_ENABLE_MOCK_UI_SIMULATION=true`, the FastAPI app serves the local mock UI at:

```text
GET /api/v1/ui
```

Legacy `/api/v1/mock-ui` remains available as a compatibility alias.

This is a demo shell for the REST `/api/v1/query` flow. It is not part of the MCP integration surface.

## Harmonise modes

The inventory service always speaks the Harmonise-style contract.

- `LOCAL_HARMONISE=true`
  - routes inventory calls to the in-process simulator in `harmonise/main.py`
  - reads mock JSON from `mock/` through the simulator transport
- `LOCAL_HARMONISE=false`
  - routes inventory calls to the real Harmonise base URL in `HTH_HARMONISE_BASE_URL`
  - passes optional headers from `HTH_HARMONISE_HEADERS`

## Testing

Run the full test suite with:

```bash
pytest
```

The tests now cover:

- REST health, tool execution, and mock UI behavior
- real MCP initialize and `tools/list` / `tools/call` flows through the official Python SDK
- structured MCP error handling for invalid args and unsupported tools
- stdio JSON-RPC framing through a subprocess smoke test

## Important files

- `app/mcp/server.py`: stdio MCP server entrypoint and lifecycle
- `app/mcp_adapter.py`: MCP tool schema/result adapter over the shared registry
- `app/tool/registry.py`: validated business tool registry
- `app/main.py`: REST-only FastAPI app
- `app/inventory/source.py`: Harmonise client with local-vs-remote transport switching
- `harmonise/main.py`: local Harmonise simulator app
