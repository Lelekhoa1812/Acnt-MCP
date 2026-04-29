from __future__ import annotations

import base64

import pytest

from app.config import Settings, build_container
from app.mcp.adapter import McpToolAdapter
from app.schemas import (
    DimensionsSnapshot,
    McpImageContent,
    MediaSnapshot,
    NormalizedEvidence,
    PricingSnapshot,
    ProductAttributeFilter,
    ProvenanceSnapshot,
    StockSnapshot,
    ToolResult,
)
from app.tool.stock.intelligence import filter_evidence_by_attributes, metric_value, rank_product_intelligence


TEST_REDIS_URL = "redis://127.0.0.1:65535"


def _evidence(
    *,
    product: str,
    variant: str,
    sku: str,
    options: list[str],
    nsw_stock: int,
    nsw_hirable: int,
    length: float,
    width: float,
    height: float,
    cost: float,
    rate: float,
) -> NormalizedEvidence:
    return NormalizedEvidence(
        product_id=f"product-{product}",
        product_name=product,
        variant_id=f"variant-{sku}",
        variant_name=variant,
        sku=sku,
        variation_options=options,
        salesNote="premium event finish",
        departmentId=3,
        categoryId="dining",
        pricing=PricingSnapshot(cost=cost, generalRate=rate),
        dimensions=DimensionsSnapshot(length=length, width=width, height=height),
        stock=StockSnapshot(nswStock=nsw_stock, nswHirable=nsw_hirable, totalStock=nsw_stock),
        media=MediaSnapshot(imageUrl=f"https://cdn.example.test/{sku}.jpg"),
        provenance=ProvenanceSnapshot(tool="test"),
    )


def test_product_intelligence_filters_and_ranks_derived_metrics() -> None:
    evidence = [
        _evidence(
            product="Dining Chair",
            variant="Gold Chair",
            sku="gold",
            options=["Gold", "Velvet"],
            nsw_stock=12,
            nsw_hirable=8,
            length=1.0,
            width=0.5,
            height=0.5,
            cost=80,
            rate=20,
        ),
        _evidence(
            product="Dining Chair",
            variant="Black Chair",
            sku="black",
            options=["Black"],
            nsw_stock=30,
            nsw_hirable=22,
            length=2.0,
            width=0.7,
            height=0.5,
            cost=120,
            rate=25,
        ),
    ]

    filtered, notes = filter_evidence_by_attributes(
        evidence,
        [ProductAttributeFilter(field="variationOption", value="Gold")],
    )
    assert notes == []
    assert [item.sku for item in filtered] == ["gold"]
    assert metric_value(filtered[0], metric="area", region="NSW") == 0.5

    rows = rank_product_intelligence(
        evidence,
        metric="volume",
        region="NSW",
        group_by="variant",
        direction="most",
        limit=1,
    )
    assert rows[0]["group"] == "Black Chair"
    assert rows[0]["rankValue"] == 0.7
    assert rows[0]["rankUnit"] == "m3"


def test_mcp_adapter_returns_text_and_image_content() -> None:
    adapter = McpToolAdapter(orchestrator_service=object(), default_session_id="test", logger=object())  # type: ignore[arg-type]
    image_data = base64.b64encode(b"fake-image").decode("ascii")
    result = adapter._success_result(
        ToolResult(
            tool="stock_image",
            data={"rows": []},
            mcp_content=[McpImageContent(data=image_data, mimeType="image/jpeg")],
        )
    )

    assert result.content[0].type == "text"
    assert result.content[1].type == "image"
    assert result.content[1].data == image_data
    assert result.content[1].mimeType == "image/jpeg"


def _settings() -> Settings:
    return Settings(
        local_harmonise=True,
        log_level="warning",
        mock_catalog_path="./mock/product-catalog.json",
        mock_details_path="./mock/product-details.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
        cloud_harmonise_image="https://images.harmonise.test",
    )


@pytest.mark.anyio
async def test_stock_specs_rank_tool_uses_mock_harmonise_without_inline_images() -> None:
    container = await build_container(_settings())
    try:
        result = await container.tool_registry.call_tool(
            "stock_specs_rank",
            {
                "search": "carpet",
                "metric": "cost",
                "groupBy": "variant",
                "direction": "most",
                "region": "NSW",
                "limit": 3,
                "attributeFilters": [{"field": "productName", "value": "carpet"}],
            },
        )
    finally:
        await container.close()

    assert result.tool == "stock_specs_rank"
    assert result.data["rows"]
    assert result.data["metric"] == "cost"
    assert result.data["coverage"]["filteredVariants"] >= len(result.data["rows"])
    assert result.mcp_content == []


@pytest.mark.anyio
async def test_stock_variant_rank_supports_non_stock_metrics_and_attribute_filters() -> None:
    container = await build_container(_settings())
    try:
        result = await container.tool_registry.call_tool(
            "stock_variant_rank",
            {
                "search": "Laminate Timber Floor",
                "metric": "cost",
                "region": "VIC",
                "direction": "least",
                "attributeFilters": [{"field": "variantName", "value": "Grey Ash"}],
                "limit": 3,
            },
        )
    finally:
        await container.close()

    assert result.tool == "stock_variant_rank"
    assert result.data["metric"] == "cost"
    assert result.data["rows"]
    assert result.data["coverage"]["filteredVariants"] >= 1
    assert all(row["groupBy"] == "variant" for row in result.data["rows"])
    assert result.data["rows"][0]["variants"][0]["variant"] == "Grey Ash"
    assert result.data["rows"][0]["rankValue"] == 23


@pytest.mark.anyio
async def test_stock_image_resolves_exact_image_file_name_and_renders_mcp_image() -> None:
    container = await build_container(_settings())

    async def fake_fetch(image_url: str) -> tuple[McpImageContent | None, str | None]:
        image_data = base64.b64encode(b"fake-image").decode("ascii")
        return McpImageContent(data=image_data, mimeType="image/jpeg"), None

    container.tool_registry._fetch_mcp_image_content = fake_fetch  # type: ignore[method-assign]
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"imageFileName": "fl-la-la-lam-1-ble.jpg"},
        )
    finally:
        await container.close()

    assert result.tool == "stock_image"
    assert result.data["source"] == "imageFileName"
    assert result.data["imageUrl"] == "https://images.harmonise.test/fl-la-la-lam-1-ble.jpg"
    assert len(result.mcp_content) == 1


@pytest.mark.anyio
async def test_stock_image_resolves_by_sku() -> None:
    container = await build_container(_settings())

    async def fake_fetch(image_url: str) -> tuple[McpImageContent | None, str | None]:
        return McpImageContent(data=base64.b64encode(b"fake-image").decode("ascii"), mimeType="image/jpeg"), None

    container.tool_registry._fetch_mcp_image_content = fake_fetch  # type: ignore[method-assign]
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"sku": "fl-la-la-lam-1-gre"},
        )
    finally:
        await container.close()

    assert result.data["source"] == "sku"
    assert result.data["sku"] == "fl-la-la-lam-1-gre"
    assert result.data["imageFileName"] == "fl-la-la-lam-1-gre.jpg"


@pytest.mark.anyio
async def test_stock_image_resolves_by_search() -> None:
    container = await build_container(_settings())

    async def fake_fetch(image_url: str) -> tuple[McpImageContent | None, str | None]:
        return McpImageContent(data=base64.b64encode(b"fake-image").decode("ascii"), mimeType="image/jpeg"), None

    container.tool_registry._fetch_mcp_image_content = fake_fetch  # type: ignore[method-assign]
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"search": "Laminate Timber Floor", "pageSize": 20},
        )
    finally:
        await container.close()

    assert result.data["source"] == "search"
    assert result.data["product"] == "Laminate Timber Floor"
    assert result.data["imageFileName"] is not None


@pytest.mark.anyio
async def test_stock_image_reports_limitations_when_image_fetch_fails() -> None:
    container = await build_container(_settings())

    async def fake_fetch(image_url: str) -> tuple[McpImageContent | None, str | None]:
        return None, f"Image could not be fetched for MCP rendering ({image_url}): HTTP 404."

    container.tool_registry._fetch_mcp_image_content = fake_fetch  # type: ignore[method-assign]
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"sku": "fl-la-la-lam-1-smo"},
        )
    finally:
        await container.close()

    assert result.mcp_content == []
    assert result.data["coverage"]["isPartial"] is True
    assert any("could not be fetched" in note.lower() for note in result.data["coverage"]["limitations"])
