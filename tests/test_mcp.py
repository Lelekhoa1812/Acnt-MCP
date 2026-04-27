from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from app.config import Settings
from app.mcp.server import build_mcp_server


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_REDIS_URL = "redis://127.0.0.1:65535"


def build_mcp_settings() -> Settings:
    return Settings(
        local_harmonise=True,
        log_level="warning",
        public_base_url=None,
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        mock_catalog_path="./mock/product-catalog.json",
        mock_details_path="./mock/product-details.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
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
        local_harmonise=True,
        log_level="warning",
        public_base_url="https://hth.example.test",
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        mock_catalog_path="./mock/product-catalog.json",
        mock_details_path="./mock/product-details.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
        mcp_bearer_token="test-mcp-token",
        mcp_oauth_enabled=True,
    )


@pytest.mark.anyio
async def test_mcp_initialize_and_list_tools() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        initialize = await client.initialize()
        tools = await client.list_tools()

    assert initialize.serverInfo.name == "hth-stock-intelligence"
    assert initialize.serverInfo.icons[0].src == "/api/v1/ui/public/hth.jpeg"
    assert initialize.serverInfo.icons[0].mimeType == "image/jpeg"
    assert initialize.serverInfo.websiteUrl == "/api/v1/ui"
    tool_names = {tool.name for tool in tools.tools}
    assert "stock.search_catalogue" in tool_names
    assert "stock.inventory_snapshot" in tool_names
    assert "resolver.disambiguate_candidates" in tool_names
    assert "weather.current" in tool_names
    assert "news.search" in tool_names
    assert "currency.convert" in tool_names

    currency_convert = next(tool for tool in tools.tools if tool.name == "currency.convert")
    assert "from" in currency_convert.inputSchema["properties"]


@pytest.mark.anyio
async def test_mcp_initialize_uses_absolute_metadata_urls_for_public_deployments() -> None:
    server = build_mcp_server(build_mcp_public_settings())

    async with create_connected_server_and_client_session(server) as client:
        initialize = await client.initialize()

    assert initialize.serverInfo.websiteUrl == "https://hth.example.test/api/v1/ui"
    assert initialize.serverInfo.icons[0].src == "https://hth.example.test/api/v1/ui/public/hth.jpeg"


@pytest.mark.anyio
async def test_mcp_cloud_mode_hides_metadata_tools() -> None:
    server = build_mcp_server(build_mcp_cloud_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        tools = await client.list_tools()

    tool_names = {tool.name for tool in tools.tools}
    assert "stock.get_departments" not in tool_names
    assert "stock.get_categories" not in tool_names
    assert "stock.search_catalogue" in tool_names


@pytest.mark.anyio
async def test_mcp_call_tool_returns_structured_inventory_payload() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock.search_catalogue",
            {"page": 1, "pageSize": 5, "search": "white gloss dance floor"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    names = [item["name"] for item in result.structuredContent["data"]["items"]]
    assert "Dance Floor - White Gloss " in names
    assert result.structuredContent["plan_status"]["status"] == "complete"
    assert result.structuredContent["memo_update"]["tool"] == "stock.search_catalogue"
    assert result.structuredContent["validation"]["actual_rows"] is not None


@pytest.mark.anyio
async def test_mcp_inventory_snapshot_returns_table_ready_rows() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool(
            "stock.inventory_snapshot",
            {"page": 1, "pageSize": 100},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["data"]["coverage"]["matchedProducts"] == 40
    row = next(item for item in result.structuredContent["data"]["rows"] if item["sku"] == "fl-ca-ca-10m")
    assert row["size"] == "1 x 1 x 0.01 m"
    assert row["stock"] is not None
    assert "total=" not in row["stock"]
    assert "Overall" in row["stock"]
    assert "in stock" in row["stock"]


@pytest.mark.anyio
async def test_mcp_invalid_args_return_is_error_instead_of_crashing() -> None:
    server = build_mcp_server(build_mcp_settings())

    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.call_tool("stock.get_product", {})

    assert result.isError is True
    assert result.structuredContent is not None
    assert "Invalid arguments for 'stock.get_product'" in result.structuredContent["error"]["message"]


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
        before = await client.call_tool("session.get_state", {})
        cleared = await client.call_tool("session.clear_state", {})
        after = await client.call_tool("session.get_state", {})

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
                    "name": "stock.search_catalogue",
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
    assert initialize["result"]["serverInfo"]["icons"][0]["src"] == "/api/v1/ui/public/hth.jpeg"
    assert initialize["result"]["serverInfo"]["websiteUrl"] == "/api/v1/ui"
    tool_names = {tool["name"] for tool in tools["result"]["tools"]}
    assert "stock.search_catalogue" in tool_names
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
