# acnt-mcp

Minimal MCP server exposing two tool families for a personal-accountant workflow:

- **FX (`fx_*`)** — exchange-rate lookups, historical rates, time series, conversion, fluctuation. Backed by [exchangeratesapi.io](https://exchangeratesapi.io) / apilayer.
- **Accounting (`accounting_*`)** — Open Collective account search, financial snapshot, and an expense workflow (create/edit/delete/process) plus low-level expense and transaction tools.

No authentication. Intended for local or private deployment behind a trusted boundary.

> **Roadmap.** Personal-accountant features — local expense/income tracking, budgets, and reports stored in a local database — will be layered on top in a follow-up. This repo currently ships the cleaned MCP shell + the two upstream-backed tool families above.

## Surfaces

- `python3 -m app.mcp.server` — stdio MCP server for Claude Code, Cursor, and other process-based MCP clients.
- `uvicorn app.main:app` — FastAPI app that mounts:
  - `GET /` — service info
  - `GET /api/v1/health` — health check
  - `GET /api/v1/tools` — list registered tools
  - `POST /api/v1/tools/call` — invoke a tool by name with JSON arguments
  - `/mcp` — Streamable HTTP MCP transport (Claude.ai / ChatGPT connectors)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in EXCHANGE_RATE_API and OPENCOLLECTIVE_PAT_TOKEN
```

## Run

```bash
# REST + Streamable HTTP MCP
uvicorn app.main:app --port 8000

# Stdio MCP
python3 -m app.mcp.server
```

## Configure as MCP client

`.mcp.json` (for Claude Code):

```json
{
  "mcpServers": {
    "acnt-mcp": {
      "command": "python3",
      "args": ["-m", "app.mcp.server"],
      "env": {
        "EXCHANGE_RATE_API": "...",
        "OPENCOLLECTIVE_PAT_TOKEN": "..."
      }
    }
  }
}
```

## Tests

```bash
pytest
```

Surviving tests:
- `tests/test_opencollective_gauntlet.py` — accounting service end-to-end against a mocked GraphQL backend.
- `tests/test_logging.py` — logging filter regression.

## Environment

See `.env.example` for the complete list. The only required keys are:

| Variable | Purpose |
| --- | --- |
| `EXCHANGE_RATE_API` | FX provider API key. Without it, `fx_*` tools fail at runtime. |
| `OPENCOLLECTIVE_PAT_TOKEN` | Open Collective Personal Access Token (needs the `expenses` scope to mutate). Without it, `accounting_*` tools fail at runtime. |

Redis is optional — the server falls back to in-process TTL storage when `HTH_REDIS_FALLBACK_ENABLED=true` (default).
