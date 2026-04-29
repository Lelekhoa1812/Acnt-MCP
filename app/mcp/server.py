from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Icon

from app.config import AppContainer, Settings, build_container, get_settings
from app.config.logging import configure_logging
from app.mcp.adapter import McpToolAdapter


MCP_SERVER_INSTRUCTIONS = (
    "Harmonise inventory MCP server. Choose tools by requested operation, not by hard-coded product keywords. "
    "Use stock_scope for supported departments/categories and categoryId routing. Use stock_snapshot for named "
    "family availability or broad variant tables. Use stock_aggregate for most/least totals by type, product "
    "family, category, region, or all inventory; it returns summed groups. Use stock_specs_rank for "
    "complex hierarchy, dimension, pricing, attribute/style, and state ranking. Use stock_variant_rank only "
    "when the user asks which variant or SKU ranks highest/lowest within a resolved family. Use stock_image "
    "for Harmonise image retrieval/rendering. Use stock_detail for exact product/SKU detail and stock_compare "
    "only for explicit 2-20 variant comparisons. For fallback, try the user's phrase first; if no rows, partial "
    "coverage, or timeouts occur, retry with a shorter distinctive phrase or a broader stock_scope filter before "
    "giving up; grouped aggregation already paginates through catalogue results in the backend. Weather, news, and FX tools are "
    "auxiliary and must not answer inventory questions. If stock tools are unavailable, use only weather, news, and FX "
    "tools instead of trying to infer inventory facts. Answer directly without preambles, tool names, or internal "
    "keys; use answer_ready or structured totals for grounding."
)


class StdioMcpApplication:
    # Motivation vs Logic: this runtime wraps the existing app container in a
    # real stdio MCP server so coding tools can speak the standard protocol
    # without duplicating any inventory, session, or external API business logic.
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        configure_logging(self.settings.log_level)
        self.logger = logging.getLogger("hth.mcp")
        self._container: AppContainer | None = None
        server_icons = [Icon(src=self.settings.resolved_server_logo_url, mimeType="image/jpeg", sizes=["225x225"])]
        self.server: Server[dict[str, Any], Any] = Server(
            name=self.settings.server_name,
            version=self.settings.server_version,
            instructions=MCP_SERVER_INSTRUCTIONS,
            website_url=self.settings.resolved_server_website_url,
            icons=server_icons,
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
            return await adapter.call_tool(
                name=name,
                arguments=arguments,
                request_context=self.server.request_context,
            )

    def _adapter(self) -> McpToolAdapter:
        container = self._require_container()
        return McpToolAdapter(
            orchestrator_service=container.orchestrator_service,
            default_session_id=container.settings.default_session_id,
            logger=self.logger,
        )

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
                instructions=(
                    MCP_SERVER_INSTRUCTIONS
                ),
                website_url=resolved_settings.resolved_server_website_url,
                icons=[
                    Icon(
                        src=resolved_settings.resolved_server_logo_url,
                        mimeType="image/jpeg",
                        sizes=["225x225"],
                    )
                ],
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
