from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings


class InMemoryTtlStore:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, str]] = {}
        self._persistent_values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        persistent_value = self._persistent_values.get(key)
        if persistent_value is not None:
            return persistent_value
        stored = self._values.get(key)
        if not stored:
            return None
        expires_at, value = stored
        if expires_at <= time.time():
            self._values.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is None:
            self._persistent_values[key] = value
            self._values.pop(key, None)
            return
        self._values[key] = (time.time() + ttl_seconds, value)

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)
        self._persistent_values.pop(key, None)


class AppKeyValueStore:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._memory = InMemoryTtlStore()

    @property
    def persistence_backend(self) -> str:
        """Always uses in-memory storage (no Redis)."""
        return "memory"

    async def connect(self) -> None:
        self.logger.debug("Using in-memory TTL storage.")

    async def close(self) -> None:
        pass

    async def get_json(self, namespace: str, key: str) -> tuple[Any | None, str]:
        namespaced_key = f"{namespace}:{key}"
        raw = await self._memory.get(namespaced_key)
        if raw is None:
            return None, "memory_miss"
        return json.loads(raw), "memory_hit"

    async def set_json(self, namespace: str, key: str, value: Any, ttl_seconds: int | None = None) -> str:
        namespaced_key = f"{namespace}:{key}"
        raw = json.dumps(value, sort_keys=True, default=str)
        await self._memory.set(namespaced_key, raw, ttl_seconds)
        return "memory_set"

    async def delete(self, namespace: str, key: str) -> str:
        namespaced_key = f"{namespace}:{key}"
        await self._memory.delete(namespaced_key)
        return "memory_delete"

    async def cached_call(
        self,
        namespace: str,
        key: str,
        ttl_seconds: int,
        loader: Callable[[], Awaitable[tuple[Any, list[str]]]],
    ) -> tuple[Any, str, list[str]]:
        cached, cache_status = await self.get_json(namespace=namespace, key=key)
        if cached is not None:
            return cached, cache_status, []
        loaded, notes = await loader()
        await self.set_json(namespace=namespace, key=key, value=loaded, ttl_seconds=ttl_seconds)
        return loaded, cache_status, notes
