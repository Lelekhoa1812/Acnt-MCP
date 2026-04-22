from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.container import build_container
from app.schemas import AgentQueryRequest

TEST_REDIS_URL = "redis://127.0.0.1:65535"


def build_engine_settings() -> Settings:
    return Settings(
        local_harmonise=True,
        log_level="warning",
        mock_catalog_path="./mock/product-catalog-enriched.json",
        mock_details_path="./mock/product-details-enriched.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
    )


@pytest.mark.anyio
async def test_agent_engine_uses_compact_snapshot_payload_for_follow_up_model_turn() -> None:
    container = await build_container(build_engine_settings())
    payloads: list[dict[str, object]] = []

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        payloads.append(payload)
        if len(payloads) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<thought>\n"
                                "goal: collect table-ready inventory evidence\n"
                                "entity_guess: category\n"
                                "strategy: catalogue search\n"
                                "tool: stock.inventory_snapshot\n"
                                "args_draft: {\"page\":1,\"pageSize\":100}\n"
                                "risk: none\n"
                                "</thought>"
                            ),
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "stock.inventory_snapshot",
                                        "arguments": "{\"page\":1,\"pageSize\":100}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if len(payloads) == 2:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "| Product | SKU |\n| --- | --- |\n| 10m Hex Carpet Set - Onyx | fl-ca-ca-10m |"
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "answered",
                                "answer": "| Product | SKU |\n| --- | --- |\n| 10m Hex Carpet Set - Onyx | fl-ca-ca-10m |",
                                "limitations": [],
                                "clarification": None,
                            }
                        )
                    }
                }
            ]
        }

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-compact")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="List all stock with sizes and specs in a table.",
                sessionId="engine-compact",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert len(payloads) >= 2

    tool_messages = [message for message in payloads[1]["messages"] if message.get("role") == "tool"]
    assert tool_messages
    tool_payload = json.loads(tool_messages[-1]["content"])
    assert "rows" in tool_payload
    assert "coverage" in tool_payload
    assert "evidence" not in tool_payload


@pytest.mark.anyio
async def test_agent_engine_renders_grounded_snapshot_when_model_never_finishes_answer() -> None:
    container = await build_container(build_engine_settings())
    calls = {"count": 0}

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<thought>\n"
                                "goal: collect table-ready inventory evidence\n"
                                "entity_guess: category\n"
                                "strategy: catalogue search\n"
                                "tool: stock.inventory_snapshot\n"
                                "args_draft: {\"page\":1,\"pageSize\":100}\n"
                                "risk: none\n"
                                "</thought>"
                            ),
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "stock.inventory_snapshot",
                                        "arguments": "{\"page\":1,\"pageSize\":100}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if calls["count"] in (2, 3, 4):
            return {"choices": [{"message": {"content": ""}}]}
        return {"choices": [{"message": {"content": "not-json"}}]}

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-limited")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="List all stock with sizes and specs in a table.",
                sessionId="engine-limited",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert "I pulled a grounded inventory snapshot" in result.answer
    assert "| Product | Variant | SKU | Colour / Finish Evidence |" in result.answer
    assert any(trace.tool == "stock.inventory_snapshot" for trace in result.tool_trace)
    assert len(result.resolved_items) == 60
    assert any("empty final assistant message" in limitation for limitation in result.limitations)
    assert any("rendered the grounded inventory snapshot directly" in limitation for limitation in result.limitations)
