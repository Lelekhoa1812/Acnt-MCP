from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agent import AgentEngine
from app.config import Settings
from app.currency import CurrencyService
from app.inventory.service import InventoryService
from app.inventory.source import HarmoniseInventorySource
from app.news import NewsService
from app.orchestrator import OrchestratorService
from app.resolver import ResolverService
from app.session.store import SessionStore
from app.store import AppKeyValueStore
from app.tool.registry import ToolRegistry
from app.weather import WeatherService


@dataclass
class AppContainer:
    settings: Settings
    key_value_store: AppKeyValueStore
    inventory_source: HarmoniseInventorySource
    inventory_service: InventoryService
    session_store: SessionStore
    resolver_service: ResolverService
    news_service: NewsService
    weather_service: WeatherService
    currency_service: CurrencyService
    tool_registry: ToolRegistry
    agent_engine: AgentEngine
    orchestrator_service: OrchestratorService

    async def close(self) -> None:
        await self.agent_engine.close()
        await self.inventory_source.close()
        await self.news_service.close()
        await self.weather_service.close()
        await self.currency_service.close()
        await self.key_value_store.close()


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

    inventory_source = HarmoniseInventorySource(settings=settings, logger=logger)

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
    tool_registry = ToolRegistry(
        inventory_service=inventory_service,
        resolver_service=resolver_service,
        session_store=session_store,
        news_service=news_service,
        weather_service=weather_service,
        currency_service=currency_service,
        logger=logger,
    )
    agent_engine = AgentEngine(settings=settings, tool_registry=tool_registry, logger=logger)
    orchestrator_service = OrchestratorService(
        settings=settings,
        agent_engine=agent_engine,
        tool_registry=tool_registry,
        session_store=session_store,
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
        tool_registry=tool_registry,
        agent_engine=agent_engine,
        orchestrator_service=orchestrator_service,
    )
