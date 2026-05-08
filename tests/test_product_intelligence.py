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
    ProductListItemDto,
    ProductListItemDtoPagedResponse,
    ProductVariantDetailsDto,
    ProductVariantDto,
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


def test_mcp_adapter_returns_image_content_before_text_fallback_contract() -> None:
    adapter = McpToolAdapter(orchestrator_service=object(), default_session_id="test", logger=object())  # type: ignore[arg-type]
    image_data = base64.b64encode(b"fake-image").decode("ascii")
    result = adapter._success_result(
        ToolResult(
            tool="stock_image",
            data={"rows": []},
            mcp_content=[McpImageContent(data=image_data, mimeType="image/jpeg")],
        )
    )

    assert result.content[0].type == "image"
    assert result.content[0].data == image_data
    assert result.content[0].mimeType == "image/jpeg"
    assert len(result.content) == 1
    assert result.isError is False
    assert result.structuredContent == {"data": {"rows": []}}


def _settings() -> Settings:
    return Settings(
        HTH_LOG_LEVEL="warning",
        LOCAL_HARMONISE=True,
        CLOUD_HARMONISE_IMAGE="https://images.harmonise.test",
        HTH_REDIS_FALLBACK_ENABLED=True,
        HTH_REDIS_URL=TEST_REDIS_URL,
        HTH_ENABLE_MOCK_UI_SIMULATION=False,
    )


@pytest.mark.anyio
async def test_stock_rank_tool_uses_mock_harmonise_without_inline_images() -> None:
    container = await build_container(_settings())
    try:
        result = await container.tool_registry.call_tool(
            "stock_rank",
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

    assert result.tool == "stock_rank"
    assert result.data["rows"]
    assert result.data["metric"] == "cost"
    assert result.data["coverage"]["filteredVariants"] >= len(result.data["rows"])
    assert result.mcp_content == []


@pytest.mark.anyio
async def test_stock_rank_supports_variant_grouping_non_stock_metrics_and_attribute_filters() -> None:
    container = await build_container(_settings())
    try:
        result = await container.tool_registry.call_tool(
            "stock_rank",
            {
                "search": "Laminate Timber Floor",
                "metric": "cost",
                "groupBy": "variant",
                "region": "VIC",
                "direction": "least",
                "attributeFilters": [{"field": "variantName", "value": "Grey Ash"}],
                "limit": 3,
            },
        )
    finally:
        await container.close()

    assert result.tool == "stock_rank"
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
    rendering = result.data["rendering"]
    assert rendering["fallbackOrder"] == [
        "encoded_mcp_image_content",
        "download_resolved_uri_local_file",
        "activated_environment_script_preview",
        "plain_best_uri",
    ]
    assert "encoded MCP image content" in rendering["steps"][0]
    assert "Automatically download bestResolvedUri locally" in rendering["steps"][1]
    assert "do not ask the user to request a script environment" in rendering["steps"][1]
    assert "creates and activates its own environment" in rendering["steps"][2]
    assert "AI client's technical rendering issue" in rendering["steps"][3]
    assert rendering["bestResolvedUri"] == "https://images.harmonise.test/fl-la-la-lam-1-ble.jpg"
    assert rendering["defaultThumbnailUri"] == "https://images.harmonise.test/fl-la-la-lam-1-ble.jpg"
    assert "Do not run scripts before trying this encoded image path" in rendering["encoded"]["instruction"]
    assert "automatically download bestResolvedUri to a local file" in rendering["localFile"]["instruction"]
    assert "automaticRenderCommand" in rendering["localFile"]
    assert "python3 -m venv" in rendering["localFile"]["automaticRenderCommand"]
    assert "browser" in rendering
    assert "htmlTemplate" in rendering["browser"]
    assert "https://images.harmonise.test/fl-la-la-lam-1-ble.jpg" in rendering["browser"]["htmlTemplate"]
    assert "desktop" in rendering
    assert "creates a temporary script environment" in rendering["desktop"]["instruction"]
    assert "Do not wait for the user to ask" in rendering["desktop"]["instruction"]
    assert "python3" in rendering["desktop"]["pythonSnippet"]
    assert "python3 -m venv" in rendering["desktop"]["automaticRenderCommand"]
    assert ". \"$HTH_STOCK_IMAGE_PREVIEW_ENV/bin/activate\"" in rendering["desktop"]["automaticRenderCommand"]
    assert rendering["uriOnly"]["bestUriToShow"] == "https://images.harmonise.test/fl-la-la-lam-1-ble.jpg"
    assert "AI client has a technical issue rendering the image inline" in rendering["uriOnly"]["instruction"]
    assert rendering["markdown"] == "![Harmonise product image](https://images.harmonise.test/fl-la-la-lam-1-ble.jpg)"
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
    assert result.data["harmoniseImageResolution"] == "details_imageFileName"


@pytest.mark.anyio
async def test_stock_image_resolves_image_thumbnail_uri_on_variant_only(monkeypatch: pytest.MonkeyPatch) -> None:
    container = await build_container(_settings())
    chair = ProductListItemDto(
        id="pg-1",
        name="Black Padded Chair",
        departmentId=3,
        categoryId="b7d70000-eacf-fc4c-c59a-08de7f19d85e",
        isActive=True,
        variants=[
            ProductVariantDto(
                id="fn-se-ch-bla",
                sku="fn-se-ch-bla",
                name="Black Padded Chair",
                imageThumbnailUri="https://blob.example/stock/thumb.png",
                details=ProductVariantDetailsDto(),
            ),
        ],
    )
    page = ProductListItemDtoPagedResponse(
        items=[chair],
        page=1,
        pageSize=20,
        totalCount=1,
        totalPages=1,
    )

    async def fake_get_product(args):  # noqa: ANN001
        return page, "bypass", []

    monkeypatch.setattr(container.tool_registry.inventory_service, "get_product", fake_get_product)

    async def fake_fetch(image_url: str) -> tuple[McpImageContent | None, str | None]:
        return McpImageContent(data=base64.b64encode(b"x").decode("ascii"), mimeType="image/png"), None

    container.tool_registry._fetch_mcp_image_content = fake_fetch  # type: ignore[method-assign]
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"sku": "fn-se-ch-bla"},
        )
    finally:
        await container.close()

    assert result.data["imageUrl"] == "https://blob.example/stock/thumb.png"
    assert result.data["harmoniseImageResolution"] == "variant_harmonise_uri"
    assert result.data["imageFileName"] == "https://blob.example/stock/thumb.png"


@pytest.mark.anyio
async def test_stock_image_promotes_thumbnail_to_first_available_high_resolution_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = await build_container(_settings())
    image_id = "0cb76216-98fd-4824-911f-c95845af2d98"
    thumb_url = f"https://blob.example/stock/product-images/{image_id}_thumb.png"
    jpg_url = f"https://blob.example/stock/product-images/{image_id}.jpg"
    chair = ProductListItemDto(
        id="pg-1",
        name="High Res Chair",
        departmentId=3,
        categoryId="cat-1",
        isActive=True,
        variants=[
            ProductVariantDto(
                id="sku-high-res",
                sku="sku-high-res",
                name="High Res Chair",
                imageThumbnailUri=thumb_url,
                details=ProductVariantDetailsDto(),
            ),
        ],
    )
    page = ProductListItemDtoPagedResponse(
        items=[chair],
        page=1,
        pageSize=20,
        totalCount=1,
        totalPages=1,
    )

    async def fake_get_product(args):  # noqa: ANN001
        return page, "bypass", []

    fetch_attempts: list[str] = []

    async def fake_fetch(image_url: str) -> tuple[McpImageContent | None, str | None]:
        fetch_attempts.append(image_url)
        if image_url == jpg_url:
            return McpImageContent(data=base64.b64encode(b"high-res").decode("ascii"), mimeType="image/jpeg"), None
        return None, f"Image could not be fetched for MCP rendering ({image_url}): HTTP 404."

    monkeypatch.setattr(container.tool_registry.inventory_service, "get_product", fake_get_product)
    container.tool_registry._fetch_mcp_image_content = fake_fetch  # type: ignore[method-assign]
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"sku": "sku-high-res"},
        )
    finally:
        await container.close()

    assert fetch_attempts == [
        f"https://blob.example/stock/product-images/{image_id}.png",
        jpg_url,
    ]
    assert result.data["imageUrl"] == jpg_url
    assert result.data["rendering"]["bestResolvedUri"] == jpg_url
    assert result.data["rendering"]["defaultThumbnailUri"] == thumb_url
    assert result.data["rendering"]["uriOnly"]["fallbackThumbnailUri"] == thumb_url
    assert result.data["rendering"]["markdown"] == f"![Harmonise product image]({jpg_url})"
    assert result.mcp_content[0].mimeType == "image/jpeg"
    assert any("higher-resolution" in note for note in result.data["resolutionNotes"])


@pytest.mark.anyio
async def test_stock_image_falls_back_to_thumbnail_when_high_resolution_candidates_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = await build_container(_settings())
    image_id = "41ccc744-b912-4b3f-82e7-cb9c6a0a464c"
    thumb_url = f"https://blob.example/stock/product-images/{image_id}_thumb.png"
    chair = ProductListItemDto(
        id="pg-1",
        name="Thumb Chair",
        departmentId=3,
        categoryId="cat-1",
        isActive=True,
        variants=[
            ProductVariantDto(
                id="sku-thumb-only",
                sku="sku-thumb-only",
                name="Thumb Chair",
                imageThumbnailUri=thumb_url,
                details=ProductVariantDetailsDto(),
            ),
        ],
    )
    page = ProductListItemDtoPagedResponse(
        items=[chair],
        page=1,
        pageSize=20,
        totalCount=1,
        totalPages=1,
    )

    async def fake_get_product(args):  # noqa: ANN001
        return page, "bypass", []

    fetch_attempts: list[str] = []

    async def fake_fetch(image_url: str) -> tuple[McpImageContent | None, str | None]:
        fetch_attempts.append(image_url)
        if image_url == thumb_url:
            return McpImageContent(data=base64.b64encode(b"thumb").decode("ascii"), mimeType="image/png"), None
        return None, f"Image could not be fetched for MCP rendering ({image_url}): HTTP 404."

    monkeypatch.setattr(container.tool_registry.inventory_service, "get_product", fake_get_product)
    container.tool_registry._fetch_mcp_image_content = fake_fetch  # type: ignore[method-assign]
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"sku": "sku-thumb-only"},
        )
    finally:
        await container.close()

    assert fetch_attempts == [
        f"https://blob.example/stock/product-images/{image_id}.png",
        f"https://blob.example/stock/product-images/{image_id}.jpg",
        f"https://blob.example/stock/product-images/{image_id}.jpeg",
        thumb_url,
    ]
    assert result.data["imageUrl"] == thumb_url
    assert result.mcp_content[0].mimeType == "image/png"


@pytest.mark.anyio
async def test_stock_image_includes_harmonise_snapshot_when_no_image(monkeypatch: pytest.MonkeyPatch) -> None:
    container = await build_container(_settings())
    chair = ProductListItemDto(
        id="pg-1",
        name="No Image Chair",
        departmentId=3,
        categoryId="cat-1",
        isActive=True,
        variants=[
            ProductVariantDto(
                id="sku-no-img",
                sku="sku-no-img",
                name="No Image Chair",
                details=ProductVariantDetailsDto(),
            ),
        ],
    )
    page = ProductListItemDtoPagedResponse(
        items=[chair],
        page=1,
        pageSize=20,
        totalCount=1,
        totalPages=1,
    )

    async def fake_get_product(args):  # noqa: ANN001
        return page, "bypass", []

    monkeypatch.setattr(container.tool_registry.inventory_service, "get_product", fake_get_product)
    try:
        result = await container.tool_registry.call_tool(
            "stock_image",
            {"sku": "sku-no-img"},
        )
    finally:
        await container.close()

    assert result.data["imageUrl"] is None
    assert "rendering" not in result.data
    assert "harmoniseSnapshotForLlm" in result.data
    assert result.data["harmoniseSnapshotForLlm"]["name"] == "No Image Chair"
    assert result.data["coverage"]["isPartial"] is True


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
