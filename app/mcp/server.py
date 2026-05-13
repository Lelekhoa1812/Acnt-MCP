from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from app.config import AppContainer, Settings, build_container, get_settings
from app.config.logging import configure_logging
from app.mcp.adapter import McpToolAdapter


MCP_SERVER_INSTRUCTIONS = (
    "acnt-mcp exposes two tool families: FX (exchange rates) and accounting (Open Collective). "
    "Use fx_symbols before FX lookups when currency labels are unclear; fx_latest/fx_history/fx_series/"
    "fx_convert/fx_fluctuation for rates and conversions. "
    "ACCOUNTING WORKFLOW — follow this sequence: "
    "(1) COLLECTIVE RESOLUTION: call accounting_collective_search to find the collective by name/slug. "
    "If resolution.status == 'not_found', call accounting_collective_list to show available collectives "
    "for the user to pick, or offer accounting_collective_create to create a new one (requires a host slug). "
    "(2) PAYEE RESOLUTION: call accounting_payee_list to find or confirm the payee account; "
    "use accounting_payee_view for full payee details; call accounting_payee_create to create a new "
    "organisation payee when not found. "
    "(3) FINANCIAL REVIEW: optionally call accounting_financial_snapshot for a reconciled "
    "balance/expenses/transactions view before creating expenses. "
    "(4) EXPENSE MANAGEMENT: call accounting_expense_workflow with action=CREATE/EDIT/DELETE/PROCESS "
    "to record or update expenses against the resolved collective. "
    "If accounting_expense_workflow or accounting_collective_create returns "
    "'Personal Access Token is missing required scope', advise the user to regenerate "
    "OPENCOLLECTIVE_PAT_TOKEN with the required scope."
)


class StdioMcpApplication:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        configure_logging(self.settings.log_level)
        self.logger = logging.getLogger("acnt.mcp")
        self._container: AppContainer | None = None
        self.server: Server[dict[str, Any], Any] = Server(
            name=self.settings.server_name,
            version=self.settings.server_version,
            instructions=MCP_SERVER_INSTRUCTIONS,
            lifespan=self._lifespan,
        )
        self._register_handlers()

    @asynccontextmanager
    async def _lifespan(self, _: Server[dict[str, Any], Any]):
        for note in self.settings.startup_notes():
            self.logger.warning("startup_note=%s", note)
        container = await build_container(self.settings)
        self._container = container
        try:
            yield {"container": container}
        finally:
            await container.close()
            self._container = None

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools():
            adapter = self._adapter()
            return adapter.list_tools()

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any] | None):
            adapter = self._adapter()
            return await adapter.call_tool(name=name, arguments=arguments)

    def _adapter(self) -> McpToolAdapter:
        container = self._require_container()
        return McpToolAdapter(tool_registry=container.tool_registry, logger=self.logger)

    def _require_container(self) -> AppContainer:
        if self._container is None:  # pragma: no cover - guarded by MCP lifespan
            raise RuntimeError("MCP container is not initialized.")
        return self._container


def build_mcp_server(settings: Settings | None = None) -> Server[dict[str, Any], Any]:
    return StdioMcpApplication(settings=settings).server


async def run_stdio_server(settings: Settings | None = None) -> None:
    resolved_settings = settings or get_settings()
    server = build_mcp_server(resolved_settings)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=resolved_settings.server_name,
                server_version=resolved_settings.server_version,
                instructions=MCP_SERVER_INSTRUCTIONS,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    asyncio.run(run_stdio_server())


if __name__ == "__main__":
    main()
