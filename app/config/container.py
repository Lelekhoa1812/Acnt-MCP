from __future__ import annotations

import logging
from dataclasses import dataclass

import anyio

from app.agent import AgentEngine
from app.config.settings import Settings
from app.stats.service import UsageStatsService
from app.tool.currency import CurrencyService
from app.tool.stock.service import InventoryService
from app.tool.stock.source import HarmoniseInventorySource
from app.tool.news import NewsService
from app.orchestrator import OrchestratorService
from app.resolver import ResolverService
from app.session.store import SessionStore
from app.store import AppKeyValueStore
from app.tool.registry import ToolRegistry
from app.tool.weather import WeatherService


@dataclass
class AppContainer:
    settings: Settings
    key_value_store: AppKeyValueStore
    inventory_source: HarmoniseInventorySource | None
    inventory_service: InventoryService | None
    session_store: SessionStore
    resolver_service: ResolverService
    news_service: NewsService
    weather_service: WeatherService
    currency_service: CurrencyService
    usage_stats_service: UsageStatsService
    tool_registry: ToolRegistry
    agent_engine: AgentEngine
    orchestrator_service: OrchestratorService
    harmonise_inventory_tools_enabled: bool

    async def close(self) -> None:
        await self.agent_engine.close()
        if self.inventory_source is not None:
            await self.inventory_source.close()
        await self.news_service.close()
        await self.weather_service.close()
        await self.currency_service.close()
        await self.key_value_store.close()


async def _probe_local_harmonise_inventory_source(
    inventory_source: HarmoniseInventorySource,
    logger: logging.Logger,
) -> bool:
    # Root Cause vs Logic: `LOCAL_HARMONISE=true` used to assume the backing
    # simulator was always healthy, which meant a broken local Harmonise setup
    # crashed startup instead of shrinking the tool surface. We probe once here
    # so the app can stay up with news/weather/currency only when stock cannot
    # be served.
    try:
        with anyio.fail_after(5):
            await inventory_source.probe()
    except Exception as exc:  # pragma: no cover - exercised via startup fallback tests
        logger.warning(
            "startup_note=LOCAL_HARMONISE=true but the Harmonise probe failed; "
            "disabling stock, resolver, and session tools: %s",
            exc,
        )
        return False
    return True


async def build_container(settings: Settings) -> AppContainer:
    logger = logging.getLogger("hth")
    key_value_store = AppKeyValueStore(settings=settings, logger=logger)
    await key_value_store.connect()
    # Motivation vs Logic: explicit log line so operators can confirm persistent
    # Redis before any session traffic (matches default no-fallback policy).
    logger.info(
        "key_value_store backend=%s redis_client_connected=%s",
        key_value_store.persistence_backend,
        key_value_store.redis_client_connected,
    )

    inventory_source: HarmoniseInventorySource | None = None
    inventory_service: InventoryService | None = None
    harmonise_inventory_tools_enabled = settings.harmonise_inventory_tools_enabled
    if harmonise_inventory_tools_enabled:
        try:
            inventory_source = HarmoniseInventorySource(settings=settings, logger=logger)
        except RuntimeError as exc:
            # Root Cause vs Logic: local deployments can ship without the
            # optional in-process Harmonise package, and the app should degrade
            # to news/weather/FX instead of failing startup before tool gating
            # has a chance to run.
            logger.warning(
                "startup_note=Harmonise inventory source unavailable; "
                "disabling stock, resolver, and session tools: %s",
                exc,
            )
            harmonise_inventory_tools_enabled = False
        else:
            if settings.local_harmonise:
                harmonise_inventory_tools_enabled = await _probe_local_harmonise_inventory_source(
                    inventory_source,
                    logger,
                )
                if not harmonise_inventory_tools_enabled:
                    await inventory_source.close()
                    inventory_source = None

    if inventory_source is not None:
        inventory_service = InventoryService(
            settings=settings,
            source=inventory_source,
            key_value_store=key_value_store,
            logger=logger,
        )
    session_store = SessionStore(settings=settings, key_value_store=key_value_store, logger=logger)
    resolver_service = ResolverService(logger=logger)
    news_service = NewsService(settings=settings, key_value_store=key_value_store, logger=logger)
    weather_service = WeatherService(settings=settings, key_value_store=key_value_store, logger=logger)
    currency_service = CurrencyService(settings=settings, key_value_store=key_value_store, logger=logger)
    usage_stats_service = UsageStatsService(settings=settings, key_value_store=key_value_store)
    tool_registry = ToolRegistry(
        settings=settings,
        inventory_service=inventory_service,
        resolver_service=resolver_service,
        session_store=session_store,
        news_service=news_service,
        weather_service=weather_service,
        currency_service=currency_service,
        logger=logger,
        inventory_tools_enabled=harmonise_inventory_tools_enabled,
    )
    agent_engine = AgentEngine(settings=settings, tool_registry=tool_registry, logger=logger)
    orchestrator_service = OrchestratorService(
        settings=settings,
        agent_engine=agent_engine,
        tool_registry=tool_registry,
        session_store=session_store,
        usage_stats_service=usage_stats_service,
        logger=logger,
    )

    return AppContainer(
        settings=settings,
        key_value_store=key_value_store,
        inventory_source=inventory_source,
        inventory_service=inventory_service,
        session_store=session_store,
        resolver_service=resolver_service,
        news_service=news_service,
        weather_service=weather_service,
        currency_service=currency_service,
        usage_stats_service=usage_stats_service,
        tool_registry=tool_registry,
        agent_engine=agent_engine,
        orchestrator_service=orchestrator_service,
        harmonise_inventory_tools_enabled=harmonise_inventory_tools_enabled,
    )
