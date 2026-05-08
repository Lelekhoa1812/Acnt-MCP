from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

from app.config import Settings, UpstreamServiceError
from app.store import AppKeyValueStore
from app.tool.ecommerce.model import EbayCategoryTreeArgs, EbayItemDetailArgs, EbayItemSearchArgs


class EcommerceService:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger
        self._client = httpx.AsyncClient(base_url=self.settings.resolved_ebay_base_url, timeout=30)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def item_search(self, args: EbayItemSearchArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("ebay_item_search", args.model_dump(mode="json", exclude_none=True), lambda: self._item_search_payload(args))

    async def item_detail(self, args: EbayItemDetailArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("ebay_item_detail", args.model_dump(mode="json", exclude_none=True), lambda: self._item_detail_payload(args))

    async def category_tree(self, args: EbayCategoryTreeArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "ebay_category_tree",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._category_tree_payload(args),
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

    async def _item_search_payload(self, args: EbayItemSearchArgs) -> tuple[dict[str, object], list[str]]:
        params: dict[str, object] = {"limit": args.limit, "offset": args.offset}
        if args.query:
            params["q"] = args.query
        if args.category_id:
            params["category_ids"] = args.category_id
        payload = await self._get("/buy/browse/v1/item_summary/search", params)
        items = payload.get("itemSummaries", []) if isinstance(payload, dict) else []
        return {
            "query": args.model_dump(mode="json", exclude_none=True),
            "marketplaceId": self.settings.ebay_marketplace_id,
            "items": items,
            "total": payload.get("total", len(items)) if isinstance(payload, dict) else len(items),
            "raw": payload,
        }, []

    async def _item_detail_payload(self, args: EbayItemDetailArgs) -> tuple[dict[str, object], list[str]]:
        payload = await self._get(
            f"/buy/browse/v1/item/{args.item_id}",
            {"fieldgroups": "PRODUCT,ADDITIONAL_SELLER_DETAILS"},
        )
        return {
            "itemId": args.item_id,
            "marketplaceId": self.settings.ebay_marketplace_id,
            "item": payload,
        }, []

    async def _category_tree_payload(self, args: EbayCategoryTreeArgs) -> tuple[dict[str, object], list[str]]:
        payload = await self._get(f"/commerce/taxonomy/v1/category_tree/{args.category_tree_id}", {})
        return {
            "categoryTreeId": args.category_tree_id,
            "marketplaceId": self.settings.ebay_marketplace_id,
            "categoryTree": payload,
        }, []

    async def _get(self, path: str, params: dict[str, object]) -> dict[str, Any]:
        response = await self._client.get(path, params=params, headers=await self._headers())
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text, request=self._request_label("GET", path, params))
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstreamServiceError(502, "eBay returned an unexpected non-object payload.", request=self._request_label("GET", path, params))
        return payload

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._token_value()}",
            "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id,
            "Accept": "application/json",
        }

    async def _token_value(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        if not self.settings.ebay_client_id or not self.settings.ebay_client_secret:
            raise UpstreamServiceError(503, "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET must be configured.")

        auth = base64.b64encode(f"{self.settings.ebay_client_id}:{self.settings.ebay_client_secret}".encode("utf-8")).decode("ascii")
        response = await self._client.post(
            "/identity/v1/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text, request="POST /identity/v1/oauth2/token")
        payload = response.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in") or 0)
        if not token or expires_in <= 0:
            raise UpstreamServiceError(502, "eBay token endpoint returned an invalid token payload.", request="POST /identity/v1/oauth2/token")
        self._token = str(token)
        self._token_expires_at = time.time() + max(60, expires_in - 60)
        return self._token

    @staticmethod
    def _request_label(method: str, path: str, params: dict[str, object]) -> str:
        query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
        return f"{method} {path}{('?' + query) if query else ''}"
