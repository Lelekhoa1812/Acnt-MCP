from __future__ import annotations

import os
import logging

import httpx
import pytest

from app.config import Settings
from app.tool.stock.media import build_harmonise_image_url
from app.tool.stock.source import HarmoniseInventorySource


RUN_LIVE_CLOUD_SMOKE = os.getenv("HTH_RUN_LIVE_CLOUD_SMOKE", "").strip().lower() in {"1", "true", "yes"}


@pytest.mark.skipif(not RUN_LIVE_CLOUD_SMOKE, reason="Set HTH_RUN_LIVE_CLOUD_SMOKE=true to run live cloud checks.")
@pytest.mark.anyio
async def test_live_cloud_harmonise_catalogue_sku_and_image_url() -> None:
    settings = Settings(local_harmonise=False)
    if not settings.cloud_harmonise_endpoint or not settings.cloud_harmonise_api:
        pytest.skip("CLOUD_HARMONISE_ENDPOINT and CLOUD_HARMONISE_API are required for live smoke checks.")

    source = HarmoniseInventorySource(settings=settings, logger=logging.getLogger("test.cloud.smoke"))
    try:
        catalogue, _ = await source.search_catalogue(
            page=1,
            page_size=50,
            search=None,
            department_id=None,
            category_id=None,
        )
        assert catalogue.get("items"), "Expected at least one product from cloud catalogue."

        resolved_sku: str | None = None
        image_file_name: str | None = None
        for product in catalogue.get("items", []):
            for variant in product.get("variants", []):
                sku = (variant.get("sku") or "").strip()
                if not sku:
                    continue
                detail_payload, _ = await source.get_product(
                    product_id=None,
                    sku=sku,
                    page=1,
                    page_size=1,
                )
                detail_items = detail_payload.get("items", [])
                if not detail_items:
                    continue
                detail_variants = detail_items[0].get("variants", []) or []
                if not detail_variants:
                    continue
                details = detail_variants[0].get("details") or {}
                candidate_image = details.get("imageFileName")
                if candidate_image:
                    resolved_sku = sku
                    image_file_name = candidate_image
                    break
            if resolved_sku:
                break

        assert resolved_sku, "Expected at least one SKU to resolve through /api/v1/products/{skuCode}."
        assert image_file_name, "Expected at least one resolved SKU to provide imageFileName."

        image_url = build_harmonise_image_url(settings.cloud_harmonise_image, image_file_name)
        assert image_url, "Expected a composed image URL from CLOUD_HARMONISE_IMAGE and imageFileName."

        async with httpx.AsyncClient(timeout=settings.harmonise_timeout_seconds) as client:
            image_response = await client.get(image_url)
        assert image_response.status_code == 200
    finally:
        await source.close()
