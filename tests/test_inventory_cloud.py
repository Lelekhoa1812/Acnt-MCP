from __future__ import annotations

import logging
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException, Query

from app.config import Settings, UpstreamServiceError
from app.inventory.service import InventoryService
from app.inventory.source import HarmoniseInventorySource
from app.schemas import StockInventorySnapshotArgs
from app.store import AppKeyValueStore


TEST_REDIS_URL = "redis://127.0.0.1:65535"


def _build_cloud_contract_app(call_log: dict[str, Any]) -> FastAPI:
    app = FastAPI()

    catalogue_items = [
        {
            "id": "prod-chair",
            "name": "Alto Chair",
            "departmentId": 3,
            "subDepartmentId": None,
            "categoryId": "cat-chair",
            "isActive": True,
            "variations": [],
            "variants": [
                {
                    "id": "var-chair-black",
                    "name": "Alto Chair - Black",
                    "sku": "fn-se-ch-alt-bla",
                    "totalHirable": 172,
                    "optionIds": ["opt-black"],
                },
                {
                    "id": "var-chair-white",
                    "name": "Alto Chair - White",
                    "sku": "fn-se-ch-alt-whi",
                    "totalHirable": 155,
                    "optionIds": ["opt-white"],
                },
            ],
        },
        {
            "id": "prod-stool",
            "name": "Alto Stool",
            "departmentId": 3,
            "subDepartmentId": None,
            "categoryId": "cat-stool",
            "isActive": True,
            "variations": [],
            "variants": [
                {
                    "id": "var-stool-black",
                    "name": "Alto Stool - Black",
                    "sku": "fn-se-st-alt-bla",
                    "totalHirable": 210,
                    "optionIds": ["opt-black"],
                }
            ],
        },
    ]

    details_by_sku = {
        "fn-se-ch-alt-bla": {
            "id": "prod-chair",
            "name": "Alto Chair",
            "departmentId": 3,
            "subDepartmentId": None,
            "categoryId": "cat-chair",
            "isActive": True,
            "variations": [],
            "variants": [
                {
                    "id": "var-chair-black",
                    "name": "Alto Chair - Black",
                    "sku": "fn-se-ch-alt-bla",
                    "totalHirable": 172,
                    "optionIds": ["opt-black"],
                    "details": {
                        "departmentId": 3,
                        "subDepartmentId": None,
                        "isActive": True,
                        "generalRate": 45.0,
                        "expoRate": 42.0,
                        "assignedCategoryId": "cat-chair",
                        "dimensional": True,
                        "canBeSoldInPortions": False,
                        "startDate": None,
                        "endDate": None,
                        "salesNote": "Black chair detail",
                        "length": 0.5,
                        "width": 0.5,
                        "height": 0.9,
                        "vicStock": 30,
                        "vicHirable": 27,
                        "nswStock": 12,
                        "nswHirable": 11,
                        "qldStock": 8,
                        "qldHirable": 7,
                        "totalStock": 50,
                        "lastUpdatedDate": "2026-04-23T00:00:00Z",
                        "imageFileName": "/stock/product-images/black-chair.png",
                        "cost": 10.0,
                        "components": [],
                    },
                }
            ],
        },
        "fn-se-ch-alt-whi": {
            "id": "prod-chair",
            "name": "Alto Chair",
            "departmentId": 3,
            "subDepartmentId": None,
            "categoryId": "cat-chair",
            "isActive": True,
            "variations": [],
            "variants": [
                {
                    "id": "var-chair-white",
                    "name": "Alto Chair - White",
                    "sku": "fn-se-ch-alt-whi",
                    "totalHirable": 155,
                    "optionIds": ["opt-white"],
                    "details": {
                        "departmentId": 3,
                        "subDepartmentId": None,
                        "isActive": True,
                        "generalRate": 45.0,
                        "expoRate": 42.0,
                        "assignedCategoryId": "cat-chair",
                        "dimensional": True,
                        "canBeSoldInPortions": False,
                        "startDate": None,
                        "endDate": None,
                        "salesNote": "White chair detail",
                        "length": 0.5,
                        "width": 0.5,
                        "height": 0.9,
                        "vicStock": 26,
                        "vicHirable": 24,
                        "nswStock": 9,
                        "nswHirable": 8,
                        "qldStock": 6,
                        "qldHirable": 6,
                        "totalStock": 41,
                        "lastUpdatedDate": "2026-04-23T00:00:00Z",
                        "imageFileName": "/stock/product-images/white-chair.png",
                        "cost": 10.0,
                        "components": [],
                    },
                }
            ],
        },
        "fn-se-st-alt-bla": {
            "id": "prod-stool",
            "name": "Alto Stool",
            "departmentId": 3,
            "subDepartmentId": None,
            "categoryId": "cat-stool",
            "isActive": True,
            "variations": [],
            "variants": [
                {
                    "id": "var-stool-black",
                    "name": "Alto Stool - Black",
                    "sku": "fn-se-st-alt-bla",
                    "totalHirable": 210,
                    "optionIds": ["opt-black"],
                    "details": {
                        "departmentId": 3,
                        "subDepartmentId": None,
                        "isActive": True,
                        "generalRate": 35.0,
                        "expoRate": 31.0,
                        "assignedCategoryId": "cat-stool",
                        "dimensional": True,
                        "canBeSoldInPortions": False,
                        "startDate": None,
                        "endDate": None,
                        "salesNote": "Stool detail",
                        "length": 0.45,
                        "width": 0.45,
                        "height": 0.75,
                        "vicStock": 40,
                        "vicHirable": 38,
                        "nswStock": 18,
                        "nswHirable": 17,
                        "qldStock": 10,
                        "qldHirable": 9,
                        "totalStock": 68,
                        "lastUpdatedDate": "2026-04-23T00:00:00Z",
                        "imageFileName": "/stock/product-images/stool-black.png",
                        "cost": 8.0,
                        "components": [],
                    },
                }
            ],
        },
    }

    def _require_api_key(x_product_api_key: str | None) -> None:
        if x_product_api_key != "cloud-key":
            raise HTTPException(status_code=401)

    def _paginate(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "items": items[start:end],
            "page": page,
            "pageSize": page_size,
            "totalCount": len(items),
            "totalPages": max(1, (len(items) + page_size - 1) // page_size),
        }

    @app.get("/api/v1/products")
    async def list_products(
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1),
        search: str | None = Query(None),
        departmentId: int | None = Query(None),
        categoryId: str | None = Query(None),
        x_product_api_key: str | None = Header(None, alias="x-product-api-key"),
    ) -> dict[str, Any]:
        _require_api_key(x_product_api_key)
        call_log["list"] = call_log.get("list", 0) + 1
        call_log.setdefault("list_params", []).append(
            {
                "page": page,
                "pageSize": pageSize,
                "search": search,
                "departmentId": departmentId,
                "categoryId": categoryId,
            }
        )

        filtered = list(catalogue_items)
        if departmentId is not None:
            filtered = [item for item in filtered if item["departmentId"] == departmentId]
        if categoryId is not None:
            filtered = [item for item in filtered if item["categoryId"] == categoryId]
        if search:
            lowered = search.lower()
            filtered = [
                item
                for item in filtered
                if lowered in item["name"].lower()
                or any(lowered in (variant.get("name") or "").lower() for variant in item["variants"])
                or any(lowered in (variant.get("sku") or "").lower() for variant in item["variants"])
            ]
        return _paginate(filtered, page=page, page_size=pageSize)

    @app.get("/api/v1/products/{sku}")
    async def get_product_by_sku(
        sku: str,
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1),
        x_product_api_key: str | None = Header(None, alias="x-product-api-key"),
    ) -> dict[str, Any]:
        _require_api_key(x_product_api_key)
        call_log.setdefault("sku", []).append(sku)
        product = details_by_sku.get(sku)
        if product is None:
            raise HTTPException(status_code=404)
        return _paginate([product], page=page, page_size=pageSize)

    return app


async def _build_cloud_source(app: FastAPI) -> HarmoniseInventorySource:
    settings = Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="cloud-key",
        cloud_harmonise_image="https://images.harmonise.test",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
    )
    source = HarmoniseInventorySource(settings=settings, logger=logging.getLogger("test.source.cloud"))
    original_client = source._client
    source._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.cloud_harmonise_endpoint,
        headers=settings.harmonise_client_headers,
        timeout=settings.harmonise_timeout_seconds,
    )
    await original_client.aclose()
    return source


@pytest.mark.anyio
async def test_cloud_source_search_and_sku_detail_use_products_contract() -> None:
    call_log: dict[str, Any] = {}
    source = await _build_cloud_source(_build_cloud_contract_app(call_log))

    try:
        payload, notes = await source.search_catalogue(
            page=1,
            page_size=10,
            search="alto",
            department_id=3,
            category_id=None,
        )
        assert notes == []
        assert payload["totalCount"] == 2
        assert payload["items"][0]["id"] == "prod-chair"
        assert call_log["list"] == 1
        assert call_log["list_params"][0]["search"] == "alto"

        by_sku, notes = await source.get_product(
            product_id=None,
            sku="fn-se-st-alt-bla",
            page=1,
            page_size=10,
        )
        assert notes == []
        assert by_sku["items"][0]["id"] == "prod-stool"
        assert by_sku["items"][0]["variants"][0]["sku"] == "fn-se-st-alt-bla"
        assert call_log["sku"] == ["fn-se-st-alt-bla"]
    finally:
        await source.close()


@pytest.mark.anyio
async def test_cloud_source_id_lookup_emulates_exact_match_and_reuses_catalogue_cache() -> None:
    call_log: dict[str, Any] = {}
    source = await _build_cloud_source(_build_cloud_contract_app(call_log))

    try:
        payload, notes = await source.get_product(
            product_id="prod-chair",
            sku=None,
            page=1,
            page_size=20,
        )
        assert "cloud_contract_id_lookup" in notes
        assert "cloud_contract_variant_hydration" in notes
        assert call_log["list"] == 1
        assert set(call_log["sku"]) == {"fn-se-ch-alt-bla", "fn-se-ch-alt-whi"}

        variants = payload["items"][0]["variants"]
        assert {variant["sku"] for variant in variants} == {"fn-se-ch-alt-bla", "fn-se-ch-alt-whi"}
        assert all(variant.get("details") is not None for variant in variants)

        catalogue_calls_before_second_lookup = call_log["list"]
        payload_second, _ = await source.get_product(
            product_id="prod-chair",
            sku=None,
            page=1,
            page_size=20,
        )
        assert payload_second["items"][0]["id"] == "prod-chair"
        assert call_log["list"] == catalogue_calls_before_second_lookup
    finally:
        await source.close()


@pytest.mark.anyio
async def test_inventory_snapshot_hydrates_multi_variant_rows_in_cloud_mode() -> None:
    call_log: dict[str, Any] = {}
    settings = Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="cloud-key",
        cloud_harmonise_image="https://images.harmonise.test",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
    )
    source = HarmoniseInventorySource(settings=settings, logger=logging.getLogger("test.service.cloud"))
    original_client = source._client
    source._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build_cloud_contract_app(call_log)),
        base_url=settings.cloud_harmonise_endpoint,
        headers=settings.harmonise_client_headers,
        timeout=settings.harmonise_timeout_seconds,
    )
    await original_client.aclose()

    key_value_store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test.service.cloud"))
    await key_value_store.connect()
    service = InventoryService(
        settings=settings,
        source=source,
        key_value_store=key_value_store,
        logger=logging.getLogger("test.service.cloud"),
    )

    try:
        snapshot, _, _ = await service.inventory_snapshot(
            StockInventorySnapshotArgs(page=1, pageSize=10, search="chair")
        )
        assert snapshot.coverage.enrichedProducts == 1
        assert snapshot.coverage.enrichedVariants == 2

        rows_by_sku = {row.sku: row for row in snapshot.rows}
        assert rows_by_sku["fn-se-ch-alt-bla"].size == "0.5 x 0.5 x 0.9 m"
        assert rows_by_sku["fn-se-ch-alt-whi"].size == "0.5 x 0.5 x 0.9 m"
        assert "Overall has 50 in stock" in (rows_by_sku["fn-se-ch-alt-bla"].stock or "")
        assert "Overall has 41 in stock" in (rows_by_sku["fn-se-ch-alt-whi"].stock or "")
    finally:
        await source.close()
        await key_value_store.close()


class _ParallelStockSource:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self._details_by_product_id = {
            "prod-a": self._product_payload("prod-a", "Parallel Chair A", "sku-a", 40),
            "prod-b": self._product_payload("prod-b", "Parallel Chair B", "sku-b", 30),
            "prod-c": self._product_payload("prod-c", "Parallel Chair C", "sku-c", 20),
        }
        self._details_by_sku = {
            "sku-a": self._details_by_product_id["prod-a"],
            "sku-b": self._details_by_product_id["prod-b"],
            "sku-c": self._details_by_product_id["prod-c"],
        }

    async def search_catalogue(
        self,
        page: int,
        page_size: int,
        search: str | None,
        department_id: int | None,
        category_id: str | None,
    ) -> tuple[dict[str, Any], list[str]]:
        items = [
            {
                "id": "prod-a",
                "name": "Parallel Chair A",
                "departmentId": 3,
                "subDepartmentId": None,
                "categoryId": "cat-chair",
                "isActive": True,
                "variations": [],
                "variants": [{"id": "var-a", "name": "Parallel Chair A", "sku": "sku-a", "totalHirable": 10, "optionIds": []}],
            },
            {
                "id": "prod-b",
                "name": "Parallel Chair B",
                "departmentId": 3,
                "subDepartmentId": None,
                "categoryId": "cat-chair",
                "isActive": True,
                "variations": [],
                "variants": [{"id": "var-b", "name": "Parallel Chair B", "sku": "sku-b", "totalHirable": 9, "optionIds": []}],
            },
            {
                "id": "prod-c",
                "name": "Parallel Chair C",
                "departmentId": 3,
                "subDepartmentId": None,
                "categoryId": "cat-chair",
                "isActive": True,
                "variations": [],
                "variants": [{"id": "var-c", "name": "Parallel Chair C", "sku": "sku-c", "totalHirable": 8, "optionIds": []}],
            },
        ]
        return {"items": items, "page": page, "pageSize": page_size, "totalCount": len(items), "totalPages": 1}, []

    async def get_product(
        self,
        product_id: str | None,
        sku: str | None,
        page: int,
        page_size: int,
    ) -> tuple[dict[str, Any], list[str]]:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await anyio.sleep(0.03)
            if product_id:
                payload = self._details_by_product_id[product_id]
            elif sku:
                payload = self._details_by_sku[sku]
            else:
                payload = {"items": [], "page": page, "pageSize": page_size, "totalCount": 0, "totalPages": 0}
            return payload, []
        finally:
            self.in_flight -= 1

    async def close(self) -> None:
        return

    def _product_payload(self, product_id: str, name: str, sku: str, total_stock: int) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": product_id,
                    "name": name,
                    "departmentId": 3,
                    "subDepartmentId": None,
                    "categoryId": "cat-chair",
                    "isActive": True,
                    "variations": [],
                    "variants": [
                        {
                            "id": f"var-{product_id}",
                            "name": name,
                            "sku": sku,
                            "totalHirable": total_stock - 2,
                            "optionIds": [],
                            "details": {
                                "departmentId": 3,
                                "subDepartmentId": None,
                                "isActive": True,
                                "generalRate": 40.0,
                                "expoRate": 35.0,
                                "assignedCategoryId": "cat-chair",
                                "dimensional": True,
                                "canBeSoldInPortions": False,
                                "startDate": None,
                                "endDate": None,
                                "salesNote": f"{name} stock detail",
                                "length": 0.5,
                                "width": 0.5,
                                "height": 0.9,
                                "vicStock": total_stock - 5,
                                "vicHirable": total_stock - 6,
                                "nswStock": 3,
                                "nswHirable": 2,
                                "qldStock": 2,
                                "qldHirable": 1,
                                "totalStock": total_stock,
                                "lastUpdatedDate": "2026-04-23T00:00:00Z",
                                "imageFileName": "/stock/product-images/parallel.png",
                                "cost": 10.0,
                                "components": [],
                            },
                        }
                    ],
                }
            ],
            "page": 1,
            "pageSize": 20,
            "totalCount": 1,
            "totalPages": 1,
        }


@pytest.mark.anyio
async def test_compare_variants_resolves_stock_lookups_in_parallel() -> None:
    settings = Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="cloud-key",
        cloud_harmonise_image="https://images.harmonise.test",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
    )
    source = _ParallelStockSource()
    key_value_store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test.service.parallel.compare"))
    await key_value_store.connect()
    service = InventoryService(
        settings=settings,
        source=source,  # type: ignore[arg-type]
        key_value_store=key_value_store,
        logger=logging.getLogger("test.service.parallel.compare"),
    )

    try:
        evidence_items, _, _ = await service.compare_variants(["sku-a", "sku-b", "sku-c"])
        assert len(evidence_items) == 3
        assert source.max_in_flight >= 2
    finally:
        await source.close()
        await key_value_store.close()


@pytest.mark.anyio
async def test_inventory_snapshot_fetches_multi_product_details_in_parallel() -> None:
    settings = Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="cloud-key",
        cloud_harmonise_image="https://images.harmonise.test",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
    )
    source = _ParallelStockSource()
    key_value_store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test.service.parallel.snapshot"))
    await key_value_store.connect()
    service = InventoryService(
        settings=settings,
        source=source,  # type: ignore[arg-type]
        key_value_store=key_value_store,
        logger=logging.getLogger("test.service.parallel.snapshot"),
    )

    try:
        snapshot, _, _ = await service.inventory_snapshot(
            StockInventorySnapshotArgs(page=1, pageSize=3, search="parallel chair")
        )
        assert snapshot.coverage.enrichedProducts == 3
        assert snapshot.coverage.enrichedVariants == 3
        assert source.max_in_flight >= 2
    finally:
        await source.close()
        await key_value_store.close()


@pytest.mark.anyio
async def test_cloud_source_wraps_transport_timeout_as_upstream_error() -> None:
    settings = Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="cloud-key",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
    )
    source = HarmoniseInventorySource(settings=settings, logger=logging.getLogger("test.service.cloud.timeout"))
    original_client = source._client

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    source._client = httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_handler),
        base_url=settings.cloud_harmonise_endpoint,
        headers=settings.harmonise_client_headers,
    )
    await original_client.aclose()

    try:
        with pytest.raises(UpstreamServiceError) as exc_info:
            await source.search_catalogue(
                page=1,
                page_size=5,
                search="alto",
                department_id=None,
                category_id=None,
            )
        assert exc_info.value.status_code == 504
        assert "timed out" in exc_info.value.detail
    finally:
        await source.close()


class _TimeoutDetailLookupSource:
    def __init__(self) -> None:
        self._catalogue_item = {
            "id": "prod-chair",
            "name": "Timeout Chair",
            "departmentId": 3,
            "subDepartmentId": None,
            "categoryId": "cat-chair",
            "isActive": True,
            "variations": [],
            "variants": [
                {
                    "id": "var-chair-timeout",
                    "name": "Timeout Chair - Sample",
                    "sku": "timeout-chair-sku",
                    "totalHirable": 0,
                    "optionIds": [],
                }
            ],
        }

    async def search_catalogue(
        self,
        page: int,
        page_size: int,
        search: str | None,
        department_id: int | None,
        category_id: str | None,
    ) -> tuple[dict[str, Any], list[str]]:
        return (
            {
                "items": [self._catalogue_item],
                "page": page,
                "pageSize": page_size,
                "totalCount": 1,
                "totalPages": 1,
            },
            [],
        )

    async def get_product(
        self,
        product_id: str | None,
        sku: str | None,
        page: int,
        page_size: int,
    ) -> tuple[dict[str, Any], list[str]]:
        raise UpstreamServiceError(
            status_code=504,
            detail="Harmonise request timed out while retrieving variant details.",
        )

    async def close(self) -> None:
        return


@pytest.mark.anyio
async def test_inventory_snapshot_reports_detail_lookup_timeouts() -> None:
    settings = Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="cloud-key",
        cloud_harmonise_image="https://images.harmonise.test",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
    )
    source = _TimeoutDetailLookupSource()
    key_value_store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test.service.timeout"))
    await key_value_store.connect()
    service = InventoryService(
        settings=settings,
        source=source,
        key_value_store=key_value_store,
        logger=logging.getLogger("test.service.timeout"),
    )

    try:
        snapshot, _, _ = await service.inventory_snapshot(
            StockInventorySnapshotArgs(page=1, pageSize=10, search="chair")
        )
        assert snapshot.coverage.enrichedProducts == 0
        assert snapshot.coverage.enrichedVariants == 0
        assert not snapshot.rows
        assert any(
            "Detail lookups failed" in limitation
            for limitation in snapshot.coverage.limitations
        )
    finally:
        await source.close()
        await key_value_store.close()
