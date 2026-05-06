from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import re
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from app.config import Settings
from app.mcp.server import MCP_SERVER_INSTRUCTIONS, build_mcp_server
from app.prompt.stock.furniture import furniture_capability_summary
from app.schemas import StockInventorySnapshotArgs, StockListCategoryArgs


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_REDIS_URL = "redis://127.0.0.1:65535"
MCP_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def test_mcp_server_instructions_guide_grouped_inventory_fallbacks() -> None:
    assert "Choose tools by requested operation" in MCP_SERVER_INSTRUCTIONS
    assert "not by hard-coded product keywords" in MCP_SERVER_INSTRUCTIONS
    assert "Use stock_aggregate for most/least totals" in MCP_SERVER_INSTRUCTIONS
    assert "Use stock_list_category before" in MCP_SERVER_INSTRUCTIONS
    assert "Use stock_variant_rank only" in MCP_SERVER_INSTRUCTIONS
    assert "grouped aggregation already paginates" in MCP_SERVER_INSTRUCTIONS
    assert "without preambles, tool names, or internal keys" in MCP_SERVER_INSTRUCTIONS


def build_mcp_settings() -> Settings:
    # Harmonise catalogue/detail calls use `.env` (CLOUD_HARMONISE_*); run MCP tests from repo root.
    return Settings(
        log_level="warning",
        public_base_url=None,
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
    )


def build_mcp_cloud_settings() -> Settings:
    return Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="test-api-key",
        cloud_harmonise_image="https://images.harmonise.test",
        log_level="warning",
        public_base_url=None,
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
    )


def build_mcp_public_settings() -> Settings:
    return Settings(
        log_level="warning",
        public_base_url="https://hth.example.test",
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
        mcp_bearer_token="test-mcp-token",
    )


@pytest.mark.anyio
async def test_mcp_initialize_and_list_tools() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        initialize = await client.initialize()
        tools = await client.list_tools()

    assert initialize.serverInfo.name == "hth-stock-intelligence"
    assert initialize.serverInfo.icons[0].src == "/api/v1/chat/public/hth.jpeg"
    assert initialize.serverInfo.icons[0].mimeType == "image/jpeg"
    assert initialize.serverInfo.websiteUrl == "/api/v1/chat"
    tool_names = {tool.name for tool in tools.tools}
    assert all(MCP_TOOL_NAME_PATTERN.fullmatch(name) for name in tool_names)
    assert all(len(name.split("_")) < 4 for name in tool_names)
    assert "stock_search" in tool_names
    assert "stock_scope" in tool_names
    assert "stock_list_category" in tool_names
    assert "stock_snapshot" in tool_names
    assert "stock_aggregate" in tool_names
    assert "stock_specs_rank" in tool_names
    assert "stock_variant_rank" in tool_names
    assert "stock_image" in tool_names
    assert "stock_disambiguate" in tool_names
    assert "weather_current" in tool_names
    assert "news_search" in tool_names
    assert "fx_convert" in tool_names
    assert "weather_resolve" not in tool_names
    assert "stock_get_departments" not in tool_names
    assert "stock_get_categories" not in tool_names
    assert "stock_get_variant_evidence" not in tool_names
    assert "stock_get_product_family_inventory" not in tool_names
    assert "product_intelligence_rank" not in tool_names
    assert "stock_rank_variants" not in tool_names
    assert "stock_rank_variants_by_stock" not in tool_names
    assert "currency_convert" not in tool_names
    assert "session_clear_state" not in tool_names

    search_catalogue = next(tool for tool in tools.tools if tool.name == "stock_search")
    assert "supported filters" in search_catalogue.description
    assert "variants" in search_catalogue.description
    assert "description" in search_catalogue.inputSchema["properties"]["departmentId"]

    list_category = next(tool for tool in tools.tools if tool.name == "stock_list_category")
    assert "broad furniture item type" in list_category.description
    assert "query" in list_category.inputSchema["properties"]

    aggregate = next(tool for tool in tools.tools if tool.name == "stock_aggregate")
    assert "Grouped stock" in aggregate.description
    assert "not single-variant" in aggregate.description
    assert "page" not in aggregate.inputSchema["properties"]
    assert "pageSize" not in aggregate.inputSchema["properties"]

    intelligence = next(tool for tool in tools.tools if tool.name == "stock_specs_rank")
    assert "physical dimensions" in intelligence.description
    assert "attribute" in intelligence.description
    assert "metric" in intelligence.inputSchema["properties"]

    rank_tool = next(tool for tool in tools.tools if tool.name == "stock_variant_rank")
    assert "pricing metrics" in rank_tool.description
    assert "metric" in rank_tool.inputSchema["properties"]

    image_tool = next(tool for tool in tools.tools if tool.name == "stock_image")
    assert "HTTP image URL" in image_tool.description
    assert "imageFileName" in image_tool.inputSchema["properties"]

    currency_convert = next(tool for tool in tools.tools if tool.name == "fx_convert")
    assert "from" in currency_convert.inputSchema["properties"]


@pytest.mark.anyio
async def test_mcp_initialize_uses_absolute_metadata_urls_for_public_deployments() -> None:
    server = build_mcp_server(build_mcp_public_settings())

    async with create_connected_server_and_client_session(server) as client:
        initialize = await client.initialize()

    assert initialize.serverInfo.websiteUrl == "https://hth.example.test/api/v1/chat"
    assert initialize.serverInfo.icons[0].src == "https://hth.example.test/api/v1/chat/public/hth.jpeg"


@pytest.mark.anyio
async def test_mcp_cloud_mode_hides_metadata_tools() -> None:
    server = build_mcp_server(build_mcp_cloud_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        tools = await client.list_tools()

    tool_names = {tool.name for tool in tools.tools}
    assert "stock_get_departments" not in tool_names
    assert "stock_get_categories" not in tool_names
    assert "stock_get_variant_evidence" not in tool_names
    assert "session_clear_state" not in tool_names
    assert "stock_scope" in tool_names
    assert "stock_search" in tool_names


@pytest.mark.anyio
async def test_mcp_call_tool_returns_structured_inventory_payload() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock_search",
            {"page": 1, "pageSize": 5, "search": "white gloss dance floor"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    names = [item["name"] for item in result.structuredContent["data"]["items"]]
    assert "Dance Floor - White Gloss " in names
    assert result.structuredContent["plan_status"]["status"] == "complete"
    assert result.structuredContent["memo_update"]["tool"] == "stock_search"
    assert result.structuredContent["validation"]["actual_rows"] is not None
    assert result.structuredContent["answer_ready"]["items"]


@pytest.mark.anyio
async def test_mcp_stock_search_rejects_unscoped_catalogue_scan() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool("stock_search", {"page": 1})

    assert result.isError is True
    assert result.structuredContent is not None
    assert "requires at least one of search, departmentId, or categoryId" in result.structuredContent["error"]["message"]


@pytest.mark.anyio
async def test_mcp_stock_snapshot_rejects_unscoped_catalogue_scan() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool("stock_snapshot", {"page": 1})

    assert result.isError is True
    assert result.structuredContent is not None
    assert "stock_snapshot requires at least one of search, departmentId, or categoryId" in result.structuredContent[
        "error"
    ]["message"]


@pytest.mark.anyio
async def test_mcp_inventory_snapshot_returns_table_ready_rows() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock_snapshot",
            {"page": 1, "departmentId": 3, "search": "chair"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["data"]["coverage"]["matchedProducts"] >= 1
    row = result.structuredContent["data"]["rows"][0]
    assert row.get("sku")
    if row.get("stock"):
        assert "total=" not in row["stock"]


@pytest.mark.anyio
async def test_mcp_supported_scope_returns_canonical_counts() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool("stock_scope", {})

    expected = furniture_capability_summary()
    assert result.isError is False
    assert result.structuredContent is not None
    data = result.structuredContent["data"]
    assert data["supported_department_count"] == expected["supported_department_count"] == 1
    assert data["mapped_furniture_category_count"] == expected["mapped_furniture_category_count"]
    assert "guidance" in data


@pytest.mark.anyio
async def test_mcp_list_category_resolves_broad_category_phrases() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        coffee = await client.call_tool("stock_list_category", {"query": "coffee tables"})
        stools = await client.call_tool("stock_list_category", {"query": "stools"})
        ottomans = await client.call_tool("stock_list_category", {"query": "ottoman chairs"})
        tables = await client.call_tool("stock_list_category", {"query": "tables", "limit": 4})

    assert coffee.isError is False
    coffee_data = coffee.structuredContent["data"]
    assert coffee_data["status"] == "matched"
    assert coffee_data["matches"][0]["name"] == "Furniture - Tables - Coffee"
    assert coffee_data["matches"][0]["categoryId"] == "b7d70000-eacf-fc4c-f320-08de7f19d96e"

    assert stools.structuredContent["data"]["matches"][0]["name"] == "Furniture - Seating - Stools"
    assert ottomans.structuredContent["data"]["matches"][0]["name"] == "Furniture - Seating - Ottomans"

    table_data = tables.structuredContent["data"]
    table_names = {match["name"] for match in table_data["matches"]}
    assert table_data["status"] == "ambiguous"
    expected_table_names = {
        "Furniture - Tables - Bar Height",
        "Furniture - Tables - Dining",
        "Furniture - Tables - Coffee",
        "Furniture - Tables - Side",
    }
    assert len(table_names & expected_table_names) >= 2


def test_stock_list_category_args_accepts_limit_20() -> None:
    args = StockListCategoryArgs(query="Service", departmentId=3, limit=20)
    assert args.limit == 20


def test_stock_inventory_snapshot_category_id_coerces_numeric_json() -> None:
    uid = "b7d70000-eacf-fc4c-4e5b-08de7f1c2101"
    str_args = StockInventorySnapshotArgs(page=1, departmentId=3, categoryId=uid)
    assert str_args.categoryId == uid
    int_args = StockInventorySnapshotArgs(page=1, departmentId=3, categoryId=42)
    assert int_args.categoryId == "42"


@pytest.mark.anyio
async def test_mcp_list_category_service_query_with_limit_20_returns_matches() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock_list_category",
            {"query": "Service", "departmentId": 3, "limit": 20},
        )

    assert result.isError is False
    data = result.structuredContent["data"]
    assert data["status"] in {"matched", "ambiguous"}
    assert len(data["matches"]) >= 1
    names = {m["name"] for m in data["matches"]}
    assert "Furniture - Service - Food Station Counters" in names


@pytest.mark.anyio
async def test_mcp_hidden_variant_alias_remains_callable() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool("stock_get_variant_evidence", {"sku": "fl-ca-ca-10m"})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["data"]["sku"] == "fl-ca-ca-10m"


@pytest.mark.anyio
async def test_mcp_product_family_inventory_returns_all_variants() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock_get_product_family_inventory",
            {"search": "Laminate Timber Floor", "pageSize": 20},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    rows = result.structuredContent["data"]["rows"]
    variants = {row["variant"] for row in rows}
    assert {"Bleached Oak", "Grey Ash", "Smoked Oak"}.issubset(variants)
    assert result.structuredContent["answer_ready"]["coverage"]["enrichedVariants"] >= 3


@pytest.mark.anyio
async def test_mcp_variant_rank_orders_region_stock() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock_variant_rank",
            {"search": "Laminate Timber Floor", "region": "VIC", "direction": "most", "metric": "stock"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    rows = result.structuredContent["data"]["rows"]
    assert len(rows) >= 3
    vic_stock = [row["rankValue"] for row in rows if row["rankValue"] is not None]
    assert vic_stock == sorted(vic_stock, reverse=True)
    assert rows[0]["region"] == "VIC"
    assert all(row["groupBy"] == "variant" for row in rows)


@pytest.mark.anyio
async def test_mcp_stock_aggregate_ranks_grouped_nsw_stock() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock_aggregate",
            {
                "search": "Laminate Timber Floor",
                "region": "NSW",
                "measure": "stock",
                "groupBy": "product",
                "direction": "most",
                "limit": 3,
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    rows = result.structuredContent["data"]["rows"]
    assert rows[0]["group"].startswith("Laminate Timber Floor")
    assert rows[0]["rankValue"] == rows[0]["stock"]["NSW"]
    assert rows[0]["rankValue"] > max(variant["stock"] for variant in rows[0]["variants"])
    assert rows[0]["variantCount"] >= 3
    assert result.structuredContent["answer_ready"]["guidance"].startswith("Rows are grouped totals")


@pytest.mark.anyio
async def test_mcp_invalid_args_return_is_error_instead_of_crashing() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool("stock_detail", {})

    assert result.isError is True
    assert result.structuredContent is not None
    assert "Invalid arguments for 'stock_detail'" in result.structuredContent["error"]["message"]


@pytest.mark.anyio
async def test_mcp_unknown_tool_returns_structured_error() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool("nope", {})

    assert result.isError is True
    assert result.structuredContent == {
        "error": {
            "message": "Unsupported tool 'nope'.",
            "type": "UnsupportedToolError",
        }
    }


@pytest.mark.anyio
async def test_mcp_connection_scoped_session_id_persists_across_calls() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        before = await client.call_tool("session_state", {})
        cleared = await client.call_tool("session_clear_state", {})
        after = await client.call_tool("session_state", {})

    before_id = before.structuredContent["data"]["session_id"]
    cleared_id = cleared.structuredContent["data"]["session_id"]
    after_id = after.structuredContent["data"]["session_id"]

    assert before.isError is False
    assert before_id.startswith("mcp-")
    assert before_id == cleared_id == after_id
    assert after.structuredContent["data"]["recent_product_names"] == []


def test_stdio_server_speaks_line_delimited_jsonrpc() -> None:
    env = os.environ.copy()
    env.update(
        {
            "LOCAL_HARMONISE": "true",
            "HTH_REDIS_FALLBACK_ENABLED": "true",
            "HTH_REDIS_URL": TEST_REDIS_URL,
            "HTH_ENABLE_MOCK_UI_SIMULATION": "false",
            "HTH_LOG_LEVEL": "WARNING",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "app.mcp.server"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    try:
        _write_jsonrpc(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[-1],
                    "capabilities": {},
                    "clientInfo": {"name": "pytest-raw", "version": "1.0.0"},
                },
            },
        )
        initialize = _read_jsonrpc(process)

        _write_jsonrpc(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        _write_jsonrpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = _read_jsonrpc(process)

        _write_jsonrpc(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "stock_search_catalogue",
                    "arguments": {"page": 1, "pageSize": 5, "search": "white gloss dance floor"},
                },
            },
        )
        tool_call = _read_jsonrpc(process)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert initialize["result"]["serverInfo"]["name"] == "hth-stock-intelligence"
    assert initialize["result"]["serverInfo"]["icons"][0]["src"].endswith("/api/v1/chat/public/hth.jpeg")
    assert initialize["result"]["serverInfo"]["websiteUrl"].endswith("/api/v1/chat")
    tool_names = {tool["name"] for tool in tools["result"]["tools"]}
    assert all(MCP_TOOL_NAME_PATTERN.fullmatch(name) for name in tool_names)
    assert "stock_search" in tool_names
    assert "stock_search_catalogue" not in tool_names
    assert tool_call["result"]["isError"] is False
    assert tool_call["result"]["structuredContent"]["data"]["items"][0]["name"] == "Dance Floor - White Gloss "


def _write_jsonrpc(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _read_jsonrpc(process: subprocess.Popen[str], timeout_seconds: float = 10.0) -> dict[str, object]:
    assert process.stdout is not None
    assert process.stderr is not None

    ready, _, _ = select.select([process.stdout], [], [], timeout_seconds)
    if not ready:
        stderr = process.stderr.read()
        raise AssertionError(f"Timed out waiting for stdio MCP output. stderr={stderr}")

    line = process.stdout.readline().strip()
    if not line:
        stderr = process.stderr.read()
        raise AssertionError(f"Expected JSON-RPC output from stdio MCP server. stderr={stderr}")
    return json.loads(line)
