from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.errors import UpstreamServiceError
from harmonise.main import create_app


class HarmoniseInventorySource:
    # Root Cause vs Logic: the old app bypassed Harmonise entirely in mock mode,
    # which meant the MCP path and the real API path exercised different code.
    # This client now always speaks the Harmonise contract and swaps only the
    # transport between remote HTTP and an in-process local simulator.
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

        client_args: dict[str, Any] = {
            "timeout": self.settings.harmonise_timeout_seconds,
            "headers": self.settings.harmonise_header_map,
        }
        if self.settings.local_harmonise:
            client_args["transport"] = httpx.ASGITransport(app=create_app(self.settings))
            client_args["base_url"] = "http://harmonise.local"
        else:
            client_args["base_url"] = self.settings.harmonise_base_url

        self._client = httpx.AsyncClient(**client_args)

    async def get_departments(self, include_inactive: bool, include_sub_departments: bool) -> tuple[list[dict[str, Any]], list[str]]:
        payload = await self._get(
            "/api/v1/common/departments",
            params={
                "includeInactive": include_inactive,
                "includeSubDepartments": include_sub_departments,
            },
        )
        return payload, []

    async def get_categories(self, page: int, page_size: int) -> tuple[dict[str, Any], list[str]]:
        payload = await self._get(
            "/api/v1/stock/categories",
            params={"page": page, "pageSize": page_size},
        )
        return payload, []

    async def search_catalogue(
        self,
        page: int,
        page_size: int,
        search: str | None,
        department_id: int | None,
        category_id: str | None,
    ) -> tuple[dict[str, Any], list[str]]:
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if search is not None:
            params["search"] = search
        if department_id is not None:
            params["departmentId"] = department_id
        if category_id is not None:
            params["categoryId"] = category_id
        payload = await self._get("/api/v1/stock/product-catalogue", params=params)
        return payload, []

    async def get_product(
        self,
        product_id: str | None,
        sku: str | None,
        page: int,
        page_size: int,
    ) -> tuple[dict[str, Any], list[str]]:
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if product_id is not None:
            params["id"] = product_id
        if sku is not None:
            params["sku"] = sku
        payload = await self._get("/api/v1/stock/products", params=params)
        return payload, []

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = await self._client.get(path, params=params)
        if response.status_code >= 400:
            raise UpstreamServiceError(status_code=response.status_code, detail=response.text)
        return response.json()
