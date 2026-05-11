from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config.settings import Settings
from app.store import AppKeyValueStore
from app.tool.accounting import AccountingService
from app.tool.currency import CurrencyService
from app.tool.registry import ToolRegistry


@dataclass
class AppContainer:
    settings: Settings
    key_value_store: AppKeyValueStore
    currency_service: CurrencyService
    accounting_service: AccountingService
    tool_registry: ToolRegistry

    async def close(self) -> None:
        await self.accounting_service.close()
        await self.currency_service.close()
        await self.key_value_store.close()


async def build_container(settings: Settings) -> AppContainer:
    logger = logging.getLogger("acnt")
    key_value_store = AppKeyValueStore(settings=settings, logger=logger)
    await key_value_store.connect()
    logger.info(
        "key_value_store backend=%s redis_client_connected=%s",
        key_value_store.persistence_backend,
        key_value_store.redis_client_connected,
    )

    currency_service = CurrencyService(settings=settings, key_value_store=key_value_store, logger=logger)
    accounting_service = AccountingService(settings=settings, key_value_store=key_value_store, logger=logger)
    tool_registry = ToolRegistry(
        currency_service=currency_service,
        accounting_service=accounting_service,
        logger=logger,
        compact_envelope=settings.mcp_compact_envelope,
    )

    return AppContainer(
        settings=settings,
        key_value_store=key_value_store,
        currency_service=currency_service,
        accounting_service=accounting_service,
        tool_registry=tool_registry,
    )
