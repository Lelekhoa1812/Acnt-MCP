from __future__ import annotations

import json

import pytest

from app.config import Settings, UpstreamServiceError, build_container
from app.schemas import AgentQueryRequest, ConversationTurn, MemoCache, MemoEntry, PlanStatus, PlanStep, ToolTrace

TEST_REDIS_URL = "redis://127.0.0.1:65535"


def build_engine_settings() -> Settings:
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
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
    )


@pytest.mark.anyio
async def test_agent_engine_uses_compact_snapshot_payload_for_follow_up_model_turn() -> None:
    container = await build_container(build_engine_settings())
    payloads: list[tuple[str, dict[str, object]]] = []

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        payloads.append((endpoint_name, payload))
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "List inventory in table form",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "inventory snapshot",
                                            "tool": "stock.inventory_snapshot",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 100},
                                            "hypotheses": ["Snapshot rows are table-ready."],
                                            "validation": None,
                                        }
                                    ],
                                    "memo": {"entries": [], "aggregates": {}},
                                    "status": "in-progress",
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 60,
                                    "actual_rows": 60,
                                    "findings": [],
                                    "ambiguity": [],
                                    "missing_statistics": [],
                                    "confidence": 0.95,
                                    "normalized_rows": [],
                                    "normalized_evidence": [],
                                    "aggregates": {},
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/composer":
            return {
                "choices": [
                    {
                        "message": {
                            "content": "| Product | SKU |\n| --- | --- |\n| 10m Hex Carpet Set - Onyx | fl-ca-ca-10m |"
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query" and payload.get("response_format"):
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
        if endpoint_name == "/api/v1/query":
            messages = payload.get("messages", [])
            has_tool_message = any(isinstance(message, dict) and message.get("role") == "tool" for message in messages)
            if not has_tool_message:
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
            return {"choices": [{"message": {"content": ""}}]}
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
    assert len(payloads) >= 4

    query_payload = next(
        payload
        for endpoint, payload in payloads
        if endpoint == "/api/v1/query" and not payload.get("response_format")
    )
    system_message = next(message["content"] for message in query_payload["messages"] if message["role"] == "system")
    assert "Session memory summary:" in system_message
    assert "conversation_history" not in system_message
    assert "memo_cache" not in system_message
    assert "\"input_schema\"" not in system_message


@pytest.mark.anyio
async def test_agent_engine_retries_initial_query_with_compact_context_on_context_length_error() -> None:
    container = await build_container(build_engine_settings())
    system_lengths: list[int] = []
    query_attempts = {"count": 0}
    context_error_detail = (
        "{"
        '"error": {"message": "Input tokens exceed the configured limit of 272000 tokens. '
        'Your messages resulted in 1281286 tokens. Please reduce the length of the messages.", '
        '"type": "invalid_request_error", "param": "messages", "code": "context_length_exceeded"}'
        "}"
    )

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "List catalogue items",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "catalogue search",
                                            "tool": "stock.search_catalogue",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 5, "search": "floor"},
                                            "hypotheses": ["Search the catalogue first."],
                                            "validation": None,
                                        }
                                    ],
                                    "memo": {"entries": [], "aggregates": {}},
                                    "status": "in-progress",
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query" and not payload.get("response_format"):
            query_attempts["count"] += 1
            system_message = next(message["content"] for message in payload["messages"] if message["role"] == "system")
            system_lengths.append(len(system_message))
            if query_attempts["count"] == 1:
                raise UpstreamServiceError(400, context_error_detail)
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<thought>\n"
                                "goal: collect catalogue results\n"
                                "entity_guess: product\n"
                                "strategy: catalogue search\n"
                                "tool: stock.search_catalogue\n"
                                "args_draft: {\"page\":1,\"pageSize\":5,\"search\":\"floor\"}\n"
                                "risk: none\n"
                                "</thought>"
                            ),
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "stock.search_catalogue",
                                        "arguments": "{\"page\":1,\"pageSize\":5,\"search\":\"floor\"}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query" and payload.get("response_format"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "answered",
                                    "answer": "Retrieved the requested catalogue items.",
                                    "limitations": [],
                                    "clarification": None,
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 5,
                                    "actual_rows": 5,
                                    "findings": [],
                                    "ambiguity": [],
                                    "missing_statistics": [],
                                    "confidence": 0.9,
                                    "normalized_rows": [],
                                    "normalized_evidence": [],
                                    "aggregates": {},
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/composer":
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Retrieved the requested catalogue items."
                        }
                    }
                ]
            }
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-context-retry")
        session_state.recent_product_names = [f"Product {index}" for index in range(5)]
        session_state.conversation_history = [
            ConversationTurn(
                role="assistant",
                content=(
                    "| Product | Variant | SKU | Size | Stock |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    + "\n".join(
                        f"| Product {index} | Variant {index} | sku-{index} | 1 x 1 m | {index} in stock |"
                        for index in range(40)
                    )
                ),
            )
        ]
        session_state.memo_cache = MemoCache(
            entries=[
                MemoEntry(
                    step_id=1,
                    tool="stock.search_catalogue",
                    args={"page": 1, "pageSize": 5, "search": "floor"},
                    rows=[
                        {
                            "product": f"Product {index}",
                            "variant": f"Variant {index}",
                            "sku": f"sku-{index}",
                            "size": "1 x 1 m",
                            "stock": f"Overall: {index} in stock",
                            "knownSpecs": ["compact row"],
                        }
                        for index in range(20)
                    ],
                    evidence=[],
                    aggregates={},
                    provenance={},
                )
            ],
            aggregates={},
        )

        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="List the floor products we have.",
                sessionId="engine-context-retry",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert query_attempts["count"] == 2
    assert len(system_lengths) == 2
    assert system_lengths[1] < system_lengths[0]
    assert "Retrieved the requested catalogue items." in result.answer


@pytest.mark.anyio
async def test_agent_engine_renders_grounded_snapshot_when_model_never_finishes_answer() -> None:
    container = await build_container(build_engine_settings())
    payloads: list[tuple[str, dict[str, object]]] = []

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        payloads.append((endpoint_name, payload))
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Inventory table",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "inventory snapshot",
                                            "tool": "stock.inventory_snapshot",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 100},
                                            "hypotheses": ["Snapshot rows are available."],
                                            "validation": None,
                                        }
                                    ],
                                    "memo": {"entries": [], "aggregates": {}},
                                    "status": "in-progress",
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 60,
                                    "actual_rows": 60,
                                    "findings": [],
                                    "ambiguity": [],
                                    "missing_statistics": [],
                                    "confidence": 0.95,
                                    "normalized_rows": [],
                                    "normalized_evidence": [],
                                    "aggregates": {},
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/composer":
            return {"choices": [{"message": {"content": ""}}]}
        if endpoint_name == "/api/v1/query" and payload.get("response_format"):
            return {"choices": [{"message": {"content": "not-json"}}]}
        if endpoint_name == "/api/v1/query":
            messages = payload.get("messages", [])
            has_tool_message = any(isinstance(message, dict) and message.get("role") == "tool" for message in messages)
            if not has_tool_message:
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
    assert "Here is a grouped inventory view" in result.answer
    assert "| Product | Variant | SKU | Colour / Finish Evidence | Size | Other Specs | Availability |" in result.answer
    assert "\n|  |" in result.answer
    assert "Harmonise data" not in result.answer
    assert "total=" not in result.answer
    assert any(trace.tool == "stock.inventory_snapshot" for trace in result.tool_trace)
    assert len(result.resolved_items) == 60
    assert any("rendered the grounded inventory snapshot directly" in limitation for limitation in result.limitations)


@pytest.mark.anyio
async def test_agent_engine_executes_next_planned_step_when_model_skips_tool_calls() -> None:
    container = await build_container(build_engine_settings())

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "List chairs with variants",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "chair catalogue search",
                                            "tool": "stock.search_catalogue",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 50, "search": "chair"},
                                            "hypotheses": ["Chair variants can be resolved from catalogue rows."],
                                            "validation": None,
                                        }
                                    ],
                                    "memo": {"entries": [], "aggregates": {}},
                                    "status": "in-progress",
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 4,
                                    "actual_rows": 4,
                                    "findings": [],
                                    "ambiguity": [],
                                    "missing_statistics": [],
                                    "confidence": 0.91,
                                    "normalized_rows": [],
                                    "normalized_evidence": [],
                                    "aggregates": {},
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query/composer":
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Here are the chairs we have and the variants returned from the catalogue snapshot."
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query" and payload.get("response_format"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "answered",
                                    "answer": "Here are the chairs we have and the variants returned from the catalogue snapshot.",
                                    "limitations": [],
                                    "clarification": None,
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query":
            return {
                "choices": [
                    {
                        "message": {
                            "content": "I can’t reliably list all chairs and their variants yet because retrieval is incomplete."
                        }
                    }
                ]
            }
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-chair-recovery")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="list all chair that we have apparently, and the variants for each chair.",
                sessionId="engine-chair-recovery",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert "chairs we have" in result.answer
    assert "retrieval is incomplete" not in result.answer
    assert any(trace.tool == "stock.search_catalogue" for trace in result.tool_trace)
    assert any("runtime executed planned step" in item for item in result.limitations)


@pytest.mark.anyio
async def test_agent_engine_does_not_leak_stock_get_product_schema_error_for_incomplete_planned_args() -> None:
    container = await build_container(build_engine_settings())

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Resolve specific product detail",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "product detail lookup",
                                            "tool": "stock.get_product",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 20},
                                            "hypotheses": ["Product detail can be fetched directly."],
                                            "validation": None,
                                        }
                                    ],
                                    "memo": {"entries": [], "aggregates": {}},
                                    "status": "in-progress",
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query" and payload.get("response_format"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "needs_clarification",
                                    "answer": "Please share the exact product SKU or product ID so I can continue safely.",
                                    "limitations": [],
                                    "clarification": {
                                        "question": "Please share the exact product SKU or product ID so I can continue safely.",
                                        "options": [],
                                    },
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query":
            return {
                "choices": [
                    {
                        "message": {
                            "content": "I need more detail before retrieval can continue."
                        }
                    }
                ]
            }
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-missing-product-identifier")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="Show product detail for that item",
                sessionId="engine-missing-product-identifier",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "needs_clarification"
    assert "product SKU or product ID" in result.answer
    assert not result.tool_trace
    assert any("requires an `id` or `sku`" in item for item in result.limitations)
    assert not any("Either 'id' or 'sku' must be provided" in item for item in result.limitations)
