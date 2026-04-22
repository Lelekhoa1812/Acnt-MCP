from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings


class InMemoryTtlStore:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        stored = self._values.get(key)
        if not stored:
            return None
        expires_at, value = stored
        if expires_at <= time.time():
            self._values.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._values[key] = (time.time() + ttl_seconds, value)

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)


class AppKeyValueStore:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._memory = InMemoryTtlStore()
        self._redis: Redis | None = None
        self._using_memory_fallback = False

    async def connect(self) -> None:
        try:
            redis_client = Redis.from_url(self.settings.redis_url, encoding="utf-8", decode_responses=True)
            await redis_client.ping()
            self._redis = redis_client
            self.logger.debug("Connected to Redis for cache/session storage.")
        except RedisError as exc:
            if not self.settings.redis_fallback_enabled:
                raise
            self._redis = None
            self._using_memory_fallback = True
            self.logger.warning(
                "Redis connection unavailable; falling back to in-memory TTL storage. reason=%s",
                exc,
            )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def get_json(self, namespace: str, key: str) -> tuple[Any | None, str]:
        namespaced_key = f"{namespace}:{key}"
        raw = await self._get_raw(namespaced_key)
        if raw is None:
            backend = "memory" if self._using_memory_fallback else "redis"
            return None, f"{backend}_miss"
        backend = "memory" if self._using_memory_fallback else "redis"
        return json.loads(raw), f"{backend}_hit"

    async def set_json(self, namespace: str, key: str, value: Any, ttl_seconds: int) -> str:
        namespaced_key = f"{namespace}:{key}"
        raw = json.dumps(value, sort_keys=True, default=str)
        backend = await self._set_raw(namespaced_key, raw, ttl_seconds)
        return backend

    async def delete(self, namespace: str, key: str) -> str:
        namespaced_key = f"{namespace}:{key}"
        if self._redis is not None:
            try:
                await self._redis.delete(namespaced_key)
                return "redis_delete"
            except RedisError as exc:
                self._using_memory_fallback = True
                self.logger.warning("Redis delete failed; switching to memory fallback. reason=%s", exc)
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

    async def _get_raw(self, key: str) -> str | None:
        if self._redis is not None:
            try:
                return await self._redis.get(key)
            except RedisError as exc:
                self._using_memory_fallback = True
                self.logger.warning("Redis get failed; switching to memory fallback. reason=%s", exc)
        return await self._memory.get(key)

    async def _set_raw(self, key: str, value: str, ttl_seconds: int) -> str:
        if self._redis is not None:
            try:
                await self._redis.set(key, value, ex=ttl_seconds)
                return "redis_set"
            except RedisError as exc:
                self._using_memory_fallback = True
                self.logger.warning("Redis set failed; switching to memory fallback. reason=%s", exc)
        await self._memory.set(key, value, ttl_seconds)
        return "memory_set"
