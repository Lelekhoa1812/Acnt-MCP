from __future__ import annotations

import json

import anyio
import pytest

from app.config import Settings, UpstreamServiceError, build_container
from app.schemas import AgentQueryRequest, ConversationTurn, MemoCache, MemoEntry, PlanStatus, PlanStep, ToolResult, ToolTrace

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
    assert result.debug is None
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
async def test_agent_engine_recovers_variant_lookup_args_when_auto_executing_planned_step() -> None:
    container = await build_container(build_engine_settings())

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Resolve chair evidence",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "catalogue pass",
                                            "tool": "stock.search_catalogue",
                                            "status": "done",
                                            "args": {"page": 1, "pageSize": 5, "search": "chair"},
                                            "hypotheses": ["Prior step already captured chair identifiers."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 2,
                                            "name": "candidate selection",
                                            "tool": "resolver.disambiguate_candidates",
                                            "status": "done",
                                            "args": {"query": "alto chair", "limit": 5},
                                            "hypotheses": ["Prior step narrowed candidates."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 3,
                                            "name": "extract evidence",
                                            "tool": "stock.extract_variant_evidence",
                                            "status": "planned",
                                            "args": {},
                                            "hypotheses": ["Variant evidence should be extracted for the selected chair."],
                                            "validation": None,
                                        },
                                    ],
                                    "memo": {"entries": [], "aggregates": {}},
                                    "status": "in-progress",
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query":
            if payload.get("response_format"):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "status": "answered",
                                        "answer": "Recovered chair evidence successfully.",
                                        "limitations": [],
                                        "clarification": None,
                                    }
                                )
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "Retrieval needs one more step before I can answer reliably."}}]}
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 1,
                                    "actual_rows": 1,
                                    "findings": [],
                                    "ambiguity": [],
                                    "missing_statistics": [],
                                    "confidence": 0.93,
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
            return {"choices": [{"message": {"content": "Recovered chair evidence successfully."}}]}
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-variant-step-recovery")
        session_state.recent_resolved_identifiers = ["fl-ca-ca-10m"]
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="Can you show the selected chair details?",
                sessionId="engine-variant-step-recovery",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert any(trace.tool == "stock.extract_variant_evidence" for trace in result.tool_trace)
    assert any("runtime executed planned step `3` directly" in item for item in result.limitations)
    assert any("Runtime recovered missing lookup args for planned step `3`" in item for item in result.limitations)
    assert not any("Invalid arguments for 'stock.extract_variant_evidence'" in item for item in result.limitations)


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


@pytest.mark.anyio
async def test_agent_engine_executes_same_turn_tool_calls_in_parallel_with_stable_trace_order() -> None:
    container = await build_container(build_engine_settings())

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Resolve two independent retrievals",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "catalogue search",
                                            "tool": "stock.search_catalogue",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 5, "search": "dance floor"},
                                            "depends_on": [],
                                            "parallel_group": 1,
                                            "hypotheses": ["Search the catalogue."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 2,
                                            "name": "product detail lookup",
                                            "tool": "stock.get_product",
                                            "status": "planned",
                                            "args": {"sku": "fl-da-dan"},
                                            "depends_on": [],
                                            "parallel_group": 1,
                                            "hypotheses": ["Fetch exact product detail."],
                                            "validation": None,
                                        },
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
                                    "expected_rows": 1,
                                    "actual_rows": 1,
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
            return {"choices": [{"message": {"content": "Completed both retrievals."}}]}
        if endpoint_name == "/api/v1/query" and payload.get("response_format"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "answered",
                                    "answer": "Completed both retrievals.",
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
                            "content": (
                                "<thought>\n"
                                "goal: resolve stock evidence\n"
                                "entity_guess: product\n"
                                "strategy: exact lookup\n"
                                "tool: stock.search_catalogue\n"
                                "args_draft: {\"page\":1,\"pageSize\":5,\"search\":\"dance floor\"}\n"
                                "risk: none\n"
                                "</thought>"
                            ),
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "stock.search_catalogue",
                                        "arguments": "{\"page\":1,\"pageSize\":5,\"search\":\"dance floor\"}",
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "stock.get_product",
                                        "arguments": "{\"sku\":\"fl-da-dan\"}",
                                    },
                                },
                            ],
                        }
                    }
                ]
            }
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    async def fake_call_tool(tool_name, raw_args, session_id=None, thought=""):  # noqa: ANN001
        if tool_name == "stock.search_catalogue":
            await anyio.sleep(0.05)
            return ToolResult(
                tool=tool_name,
                data={"items": [{"id": "prod-1", "name": "Dance Floor", "variants": [{"id": "var-1", "sku": "fl-da-dan"}]}]},
                trace=ToolTrace(
                    thought=thought,
                    tool=tool_name,
                    args=raw_args,
                    status="ok",
                    result_count=1,
                ),
            )
        if tool_name == "stock.get_product":
            await anyio.sleep(0.01)
            return ToolResult(
                tool=tool_name,
                data={"items": [{"id": "prod-1", "name": "Dance Floor", "variants": [{"id": "var-1", "sku": "fl-da-dan"}]}]},
                trace=ToolTrace(
                    thought=thought,
                    tool=tool_name,
                    args=raw_args,
                    status="ok",
                    result_count=1,
                ),
            )
        raise AssertionError(f"Unexpected tool call: {tool_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]
    container.tool_registry.call_tool = fake_call_tool  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-parallel")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="Resolve dance floor stock and the exact product detail.",
                sessionId="engine-parallel",
                includeThoughts=True,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert [trace.tool for trace in result.tool_trace] == ["stock.search_catalogue", "stock.get_product"]
    assert result.debug is not None
    assert result.debug.retrieval.parallel_batches[0].execution_mode == "parallel"
    assert result.debug.retrieval.parallel_batches[0].tools == ["stock.search_catalogue", "stock.get_product"]


@pytest.mark.anyio
async def test_agent_engine_adds_recursive_follow_up_step_when_catalogue_result_is_thin() -> None:
    container = await build_container(build_engine_settings())
    query_calls = {"count": 0}

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Resolve white gloss dance floor size",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "catalogue search",
                                            "tool": "stock.search_catalogue",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 5, "search": "white gloss dance floor"},
                                            "depends_on": [],
                                            "parallel_group": None,
                                            "hypotheses": ["Resolve the floor from catalogue results first."],
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
                                    "expected_rows": 1,
                                    "actual_rows": 1,
                                    "findings": [],
                                    "ambiguity": [],
                                    "missing_statistics": [],
                                    "confidence": 0.94,
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
            return {"choices": [{"message": {"content": "The white gloss dance floor size is grounded in the retrieved detail."}}]}
        if endpoint_name == "/api/v1/query" and payload.get("response_format"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "answered",
                                    "answer": "The white gloss dance floor size is grounded in the retrieved detail.",
                                    "limitations": [],
                                    "clarification": None,
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query":
            query_calls["count"] += 1
            if query_calls["count"] == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "<thought>\n"
                                    "goal: resolve floor candidate\n"
                                    "entity_guess: variant\n"
                                    "strategy: catalogue search\n"
                                    "tool: stock.search_catalogue\n"
                                    "args_draft: {\"page\":1,\"pageSize\":5,\"search\":\"white gloss dance floor\"}\n"
                                    "risk: ambiguity\n"
                                    "</thought>"
                                ),
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "stock.search_catalogue",
                                            "arguments": "{\"page\":1,\"pageSize\":5,\"search\":\"white gloss dance floor\"}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "I need one more retrieval hop before I can answer safely."}}]}
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-recursive-follow-up")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="What size is the white gloss dance floor?",
                sessionId="engine-recursive-follow-up",
                includeThoughts=True,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert any(trace.tool == "stock.search_catalogue" for trace in result.tool_trace)
    assert any(trace.tool == "stock.extract_variant_evidence" for trace in result.tool_trace)
    assert len(result.plan_status.steps) >= 2
    assert result.plan_status.steps[1].depends_on == [1]
    assert any("Recursive follow-up step `2` was added" in item for item in result.limitations)
    assert result.debug is not None
