from __future__ import annotations

from typing import Any

import pytest

from app.auth.models import UserContext
from app.config.errors import UpstreamServiceError
from app.config.settings import Settings
from app.render.stats import render_usage_stats_html
from app.schemas import ToolTrace
from app.stats.models import UsageStatsSnapshot, UsageToolErrorSummary, UsageUserGroup
from app.stats.service import UsageStatsService


class FakeKeyValueStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}

    async def get_json(self, namespace: str, key: str) -> tuple[Any, str]:
        return self.values.get((namespace, key)), "memory"

    async def set_json(self, *, namespace: str, key: str, value: Any, ttl_seconds: int | None) -> None:
        self.values[(namespace, key)] = value


@pytest.mark.asyncio
async def test_usage_stats_snapshot_filters_groups_and_summarizes_ai_clients() -> None:
    settings = Settings(
        oauth_user_group="SG-HTH-MCP-Users",
        news_pl_group="all",
        weather_pl_group="all",
        currency_pl_group="all",
        stock_pl_group="Stock Team",
    )
    store = FakeKeyValueStore()
    service = UsageStatsService(settings=settings, key_value_store=store)  # type: ignore[arg-type]
    user_context = UserContext(
        tenant_id="tenant-1",
        user_id="user-1",
        subject="user-1",
        oid="user-1",
        email="liam@example.com",
        client_id="openai-mcp",
        groups=["group-id-1", "random-group-id"],
        group_names=["SG-HTH-MCP-Users", "Unrelated Group"],
    )

    await service.record_tool_call(
        user_context=user_context,
        tool_name="stock_image",
        client_id="claude-client",
        client_name="Claude",
    )
    await service.record_tool_call(
        user_context=user_context,
        tool_name="stock_image",
        client_id="openai-mcp",
        client_name="ChatGPT",
    )
    await service.record_tool_call(
        user_context=user_context,
        tool_name="stock_detail",
        client_id="cursor-client",
        client_name="Cursor",
    )

    snapshot = await service.snapshot()
    group = snapshot.groups[0]

    assert group.matched_groups == ["SG-HTH-MCP-Users"]
    assert "Unrelated Group" not in group.matched_groups
    assert [(client.label, client.ai_key, client.count) for client in group.clients] == [
        ("Cursor (cursor-client)", "cursor", 1),
        ("ChatGPT (openai-mcp)", "chatgpt", 1),
        ("Claude (claude-client)", "claude", 1),
    ]
    assert group.tools[0].name == "stock_image"
    assert group.tools[0].count == 2
    assert [(client.ai_key, client.count) for client in group.tools[0].clients] == [("chatgpt", 1), ("claude", 1)]


def test_usage_stats_extracts_repeated_tool_calls_for_counts() -> None:
    service = UsageStatsService(settings=Settings(), key_value_store=FakeKeyValueStore())  # type: ignore[arg-type]

    tool_names = service._extract_tool_names(
        [
            ToolTrace(thought="", tool="stock_image", status="ok"),
            ToolTrace(thought="", tool="stock_image", status="ok"),
            ToolTrace(thought="", tool="stock_detail", status="ok"),
        ]
    )

    assert tool_names == ["stock_image", "stock_image", "stock_detail"]


@pytest.mark.asyncio
async def test_usage_stats_records_tool_errors_with_triggering_request() -> None:
    store = FakeKeyValueStore()
    service = UsageStatsService(settings=Settings(), key_value_store=store)  # type: ignore[arg-type]
    user_context = UserContext(
        tenant_id="tenant-1",
        user_id="user-1",
        subject="user-1",
        oid="user-1",
        email="liam@example.com",
    )

    await service.record_tool_error(
        user_context=user_context,
        tool_name="stock_detail",
        tool_args={"id": "abc-def"},
        error=UpstreamServiceError(404, "Not found", request="GET /api/v1/products/abc-def"),
        client_name="Claude",
    )

    snapshot = await service.snapshot()
    error = snapshot.tool_errors[0]

    assert error.tool_name == "stock_detail"
    assert error.error_status_code == 404
    assert error.error_request == "GET /api/v1/products/abc-def"
    assert error.query == "stock_detail {'id': 'abc-def'}"
    assert error.ai_key == "claude"


@pytest.mark.asyncio
async def test_usage_stats_query_error_trace_records_user_query() -> None:
    store = FakeKeyValueStore()
    service = UsageStatsService(settings=Settings(), key_value_store=store)  # type: ignore[arg-type]

    await service.record_query(
        user_context=None,
        query="show me product abc-def",
        tool_trace=[
            ToolTrace(
                thought="",
                tool="stock_detail",
                args={"id": "abc-def"},
                status="error",
                error_status_code=404,
                error_request="GET /api/v1/products/abc-def",
                normalization_notes=["Not found"],
            )
        ],
        client_name="ChatGPT",
    )

    snapshot = await service.snapshot()
    error = snapshot.tool_errors[0]

    assert error.query == "show me product abc-def"
    assert error.error_request == "GET /api/v1/products/abc-def"
    assert error.error_message == "Not found"


def test_usage_stats_render_hides_anonymous_users_but_shows_errors() -> None:
    html = render_usage_stats_html(
        UsageStatsSnapshot(
            generated_at=1,
            groups=[UsageUserGroup(identity_label="Anonymous user")],
            tool_errors=[
                UsageToolErrorSummary(
                    recorded_at=1,
                    identity_label="Anonymous user",
                    client_label="Claude",
                    ai_key="claude",
                    tool_name="stock_detail",
                    query="show abc-def",
                    error_request="GET /api/v1/products/abc-def",
                    error_status_code=404,
                    error_message="Not found",
                )
            ],
        )
    )

    assert "<h2 class=\"group-title\">Anonymous user</h2>" not in html
    assert "No registered usage yet" in html
    assert "GET /api/v1/products/abc-def" in html
