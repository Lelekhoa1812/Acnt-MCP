from __future__ import annotations

import json
import logging

import httpx

from app.config import Settings, UpstreamServiceError
from app.tool.news.model import NewsHeadlinesArgs, NewsSearchArgs, NewsSourcesArgs
from app.store import AppKeyValueStore


class NewsService:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger
        self._client = httpx.AsyncClient(
            base_url="https://newsapi.org/v2",
            headers={"X-Api-Key": self.settings.news_api_key or ""},
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, args: NewsSearchArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "news.search",
            args.model_dump(mode="json", by_alias=True, exclude_none=True),
            lambda: self._get(
                "/everything",
                args.model_dump(mode="json", by_alias=True, exclude_none=True),
            ),
        )

    async def headlines(self, args: NewsHeadlinesArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "news.headlines",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._get("/top-headlines", args.model_dump(mode="json", exclude_none=True)),
        )

    async def sources(self, args: NewsSourcesArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "news.sources",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._get("/top-headlines/sources", args.model_dump(mode="json", exclude_none=True)),
        )

    async def _cached(
        self,
        namespace: str,
        payload: dict[str, object],
        loader,
    ) -> tuple[dict[str, object], str, list[str]]:
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"{namespace}:{json.dumps(payload, sort_keys=True, default=str)}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=loader,
        )
        return raw, cache_status, notes

    async def _get(self, path: str, params: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        if not self.settings.news_api_key:
            raise UpstreamServiceError(503, "NEWS_API is not configured in the environment.")
        response = await self._client.get(path, params=params)
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text)
        payload = response.json()
        if payload.get("status") == "error":
            raise UpstreamServiceError(response.status_code or 400, json.dumps(payload))
        return payload, []
