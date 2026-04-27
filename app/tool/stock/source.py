from __future__ import annotations

import logging
from typing import Any

import anyio
import httpx

from app.config import Settings, UpstreamServiceError


class HarmoniseInventorySource:
    # Root Cause vs Logic: the old app bypassed Harmonise entirely in mock mode,
    # which meant the MCP path and the real API path exercised different code.
    # This client now always speaks the Harmonise contract and swaps only the
    # transport between remote HTTP and an in-process local simulator.
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._catalogue_index: dict[str, dict[str, Any]] = {}
        self._catalogue_scan_complete = False
        # Root Cause vs Logic: cloud list/search GETs 500 when multiple requests
        # hit /api/v1/products concurrently. Queue all Harmonise HTTP through one
        # semaphore; local mode uses a much higher cap so in-process tests stay fast.
        if settings.local_harmonise:
            _limit = max(settings.harmonise_concurrent_request_limit, 32)
        else:
            _limit = max(1, settings.harmonise_concurrent_request_limit)
        self._http_concurrency = anyio.Semaphore(_limit)

        client_args: dict[str, Any] = {
            "timeout": self.settings.harmonise_timeout_seconds,
            "headers": self.settings.harmonise_client_headers,
        }
        if self.settings.local_harmonise:
            # Motivation vs Logic: Docker builds deploying to remote cloud omit the
            # @harmonise simulator, so only import it when the local mode is actually
            # enabled; remote deployments skip this branch entirely.
            try:
                from harmonise.main import create_app
            except ImportError as exc:
                raise RuntimeError(
                    "LOCAL_HARMONISE=true requires the @harmonise dependency; install it or disable local mode."
                ) from exc
            client_args["transport"] = httpx.ASGITransport(app=create_app(self.settings))
            client_args["base_url"] = self.settings.local_harmonise_endpoint
        else:
            client_args["base_url"] = self.settings.cloud_harmonise_endpoint

        self._client = httpx.AsyncClient(**client_args)

    async def get_departments(self, include_inactive: bool, include_sub_departments: bool) -> tuple[list[dict[str, Any]], list[str]]:
        if not self.settings.local_harmonise:
            raise UpstreamServiceError(
                status_code=501,
                detail=(
                    "Department metadata is unavailable in cloud Harmonise mode. "
                    "Use product catalogue tools for scoped product Q&A."
                ),
            )
        payload = await self._get(
            "/api/v1/common/departments",
            params={
                "includeInactive": include_inactive,
                "includeSubDepartments": include_sub_departments,
            },
        )
        return payload, []

    async def get_categories(self, page: int, page_size: int) -> tuple[dict[str, Any], list[str]]:
        if not self.settings.local_harmonise:
            raise UpstreamServiceError(
                status_code=501,
                detail=(
                    "Category metadata is unavailable in cloud Harmonise mode. "
                    "Use stock_search_catalogue with supported cloud query filters."
                ),
            )
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
        # Motivation vs Logic: cloud Harmonise uses `/api/v1/products` for
        # catalogue search while local dev keeps stock endpoints for backwards
        # compatibility. We prefer the cloud-style contract and only fall back
        # to legacy local routes if needed.
        try:
            payload = await self._get("/api/v1/products", params=params)
        except UpstreamServiceError as exc:
            if not self.settings.local_harmonise or exc.status_code not in {404, 405}:
                raise
            payload = await self._get("/api/v1/stock/product-catalogue", params=params)

        paged = self._as_paged_payload(payload, page=page, page_size=page_size)
        self._remember_catalogue_items(paged.get("items", []))
        return paged, []

    async def get_product(
        self,
        product_id: str | None,
        sku: str | None,
        page: int,
        page_size: int,
    ) -> tuple[dict[str, Any], list[str]]:
        if sku:
            # Root Cause vs Logic: /api/v1/products/{sku} is a single-resource
            # detail endpoint that does not accept pagination query params.
            # Passing page/pageSize causes a 500 on the cloud Harmonise backend;
            # Postman confirms the bare URL works correctly without any params.
            payload = await self._get(f"/api/v1/products/{sku}", params={})
            paged = self._as_paged_payload(payload, page=page, page_size=page_size)
            self._remember_catalogue_items(paged.get("items", []))
            return paged, []

        if product_id:
            return await self._get_product_by_id(
                product_id=product_id,
                page=page,
                page_size=page_size,
            )

        raise UpstreamServiceError(400, "Either product id or sku must be provided for product retrieval.")

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = await self._request_with_retry(path=path, params=params)
        if response.status_code >= 400:
            raise UpstreamServiceError(status_code=response.status_code, detail=response.text)
        return response.json()

    async def _request_with_retry(self, path: str, params: dict[str, Any]) -> httpx.Response:
        # Root Cause vs Logic: cloud Harmonise list/detail calls can run for a long
        # time under load, and strict timeout+attempt caps were truncating inventory
        # retrieval before rows were fully hydrated. When timeout is disabled in
        # settings, timeout retries now remain unbounded with capped backoff.
        max_attempts = max(1, self.settings.harmonise_max_attempts)
        unbounded_timeout_retries = self.settings.harmonise_timeout_seconds is None
        attempt = 1
        while True:
            try:
                # Hold concurrency only for the actual socket read so retries and
                # backoff do not block every other Harmonise call.
                async with self._http_concurrency:
                    response = await self._client.get(path, params=params)
            except httpx.ReadTimeout as exc:
                if unbounded_timeout_retries or attempt < max_attempts:
                    await self._sleep_before_retry(
                        path=path,
                        attempt=attempt,
                        max_attempts=None if unbounded_timeout_retries else max_attempts,
                        reason="read_timeout",
                    )
                    attempt += 1
                    continue
                raise UpstreamServiceError(
                    status_code=504,
                    detail=f"Harmonise request timed out for path '{path}' after {max_attempts} attempts.",
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < max_attempts and self._should_retry_transport_error(exc):
                    await self._sleep_before_retry(
                        path=path,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        reason=exc.__class__.__name__,
                    )
                    attempt += 1
                    continue
                raise UpstreamServiceError(
                    status_code=502,
                    detail=f"Harmonise request failed for path '{path}': {exc}",
                ) from exc

            if response.status_code in {500, 502, 503, 504} and attempt < max_attempts:
                await self._sleep_before_retry(
                    path=path,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=f"status_{response.status_code}",
                )
                attempt += 1
                continue

            return response

        # Defensive fallback (the loop should always return or raise above).
        raise UpstreamServiceError(
            status_code=502,
            detail=f"Harmonise request failed for path '{path}' after retry attempts were exhausted.",
        )

    def _should_retry_transport_error(self, exc: httpx.HTTPError) -> bool:
        return isinstance(exc, httpx.TransportError)

    def _retry_backoff_seconds(self, attempt: int) -> float:
        base_seconds = self.settings.harmonise_retry_backoff_seconds
        if base_seconds <= 0:
            return 0.0
        cap_seconds = max(base_seconds, self.settings.harmonise_retry_backoff_cap_seconds)
        return min(base_seconds * (2 ** max(0, attempt - 1)), cap_seconds)

    async def _sleep_before_retry(
        self,
        *,
        path: str,
        attempt: int,
        max_attempts: int | None,
        reason: str,
    ) -> None:
        delay_seconds = self._retry_backoff_seconds(attempt)
        max_attempts_label = "inf" if max_attempts is None else str(max_attempts)
        self.logger.warning(
            "Retrying Harmonise request path=%s reason=%s attempt=%s/%s delay=%.2fs",
            path,
            reason,
            attempt + 1,
            max_attempts_label,
            delay_seconds,
        )
        if delay_seconds > 0:
            await anyio.sleep(delay_seconds)

    async def _get_product_by_id(self, product_id: str, page: int, page_size: int) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = ["cloud_contract_id_lookup"]
        catalogue_item = await self._resolve_catalogue_item(product_id=product_id)
        if catalogue_item is None:
            # Root Cause vs Logic: cloud `/api/v1/products` ignores id/sku query
            # params, so product-id retrieval must emulate exact matching by
            # scanning catalogue pages before hydrating variants by SKU.
            raise UpstreamServiceError(
                status_code=404,
                detail=f"No exact product record matched id '{product_id}'.",
            )

        hydrated = await self._hydrate_product_by_sku(catalogue_item)
        if hydrated != catalogue_item:
            notes.append("cloud_contract_variant_hydration")
        return self._single_item_page(hydrated, page=page, page_size=page_size), notes

    async def _resolve_catalogue_item(self, product_id: str) -> dict[str, Any] | None:
        if product_id in self._catalogue_index:
            return self._catalogue_index[product_id]
        if self._catalogue_scan_complete:
            return None

        page = 1
        page_size = 100
        while True:
            payload = await self._get("/api/v1/products", params={"page": page, "pageSize": page_size})
            paged = self._as_paged_payload(payload, page=page, page_size=page_size)
            self._remember_catalogue_items(paged.get("items", []))

            if product_id in self._catalogue_index:
                return self._catalogue_index[product_id]

            total_pages = max(1, int(paged.get("totalPages") or 1))
            if page >= total_pages:
                self._catalogue_scan_complete = True
                return None
            page += 1

    async def _hydrate_product_by_sku(self, product: dict[str, Any]) -> dict[str, Any]:
        variants = product.get("variants", []) or []
        if not variants:
            return product

        expected_skus = self._unique_variant_skus(variants)
        if not expected_skus:
            return product

        hydrated_variants: dict[str, dict[str, Any]] = {}
        detail_product: dict[str, Any] | None = None
        # Root Cause vs Logic: cloud hydration previously called `/api/v1/products/{sku}`
        # for every variant even when the first response already contained full
        # product-level variant details, which amplified remote latency and timeouts.
        # We now short-circuit once all expected SKUs have hydrated detail payloads.
        for sku in expected_skus:
            # Root Cause vs Logic: match get_product(sku): the cloud detail route
            # returns 500 when page/pageSize query params are present; see get_product.
            payload = await self._get(f"/api/v1/products/{sku}", params={})
            paged = self._as_paged_payload(payload, page=1, page_size=1)
            items = paged.get("items", []) or []
            if not items:
                continue
            candidate_product = items[0]
            if detail_product is None:
                detail_product = candidate_product
            for detail_variant in candidate_product.get("variants", []) or []:
                detail_sku = detail_variant.get("sku")
                if detail_sku:
                    hydrated_variants[detail_sku] = detail_variant

            if self._has_complete_variant_hydration(
                expected_skus=expected_skus,
                hydrated_variants=hydrated_variants,
            ):
                break

        if not hydrated_variants:
            return product

        merged_product = dict(product)
        if detail_product is not None:
            for key, value in detail_product.items():
                if key == "variants":
                    continue
                if value is not None:
                    merged_product[key] = value

        merged_variants: list[dict[str, Any]] = []
        seen_skus: set[str] = set()
        for variant in variants:
            sku = variant.get("sku")
            if sku and sku in hydrated_variants:
                merged = dict(hydrated_variants[sku])
                for key, value in variant.items():
                    merged.setdefault(key, value)
            else:
                merged = dict(variant)
            merged_variants.append(merged)
            if sku:
                seen_skus.add(sku)

        for sku, hydrated_variant in hydrated_variants.items():
            if sku in seen_skus:
                continue
            merged_variants.append(dict(hydrated_variant))

        merged_product["variants"] = merged_variants
        return merged_product

    def _unique_variant_skus(self, variants: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for variant in variants:
            sku = variant.get("sku")
            if not isinstance(sku, str):
                continue
            compact = sku.strip()
            if not compact or compact in seen:
                continue
            seen.add(compact)
            ordered.append(compact)
        return ordered

    def _has_complete_variant_hydration(
        self,
        *,
        expected_skus: list[str],
        hydrated_variants: dict[str, dict[str, Any]],
    ) -> bool:
        for sku in expected_skus:
            variant = hydrated_variants.get(sku)
            if not isinstance(variant, dict):
                return False
            if variant.get("details") is None:
                return False
        return True

    def _remember_catalogue_items(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            product_id = item.get("id")
            if product_id:
                self._catalogue_index[str(product_id)] = item

    def _single_item_page(self, item: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
        include_item = page == 1 and page_size >= 1
        return {
            "items": [item] if include_item else [],
            "page": page,
            "pageSize": page_size,
            "totalCount": 1,
            "totalPages": 1,
        }

    def _as_paged_payload(self, payload: Any, page: int, page_size: int) -> dict[str, Any]:
        if isinstance(payload, dict) and "items" in payload:
            return payload
        if isinstance(payload, dict) and "id" in payload:
            return self._single_item_page(payload, page=page, page_size=page_size)
        return {
            "items": [],
            "page": page,
            "pageSize": page_size,
            "totalCount": 0,
            "totalPages": 0,
        }
