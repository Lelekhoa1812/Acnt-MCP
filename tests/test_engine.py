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
    assert len(payloads) >= 3

    query_payload = next(
        payload
        for endpoint, payload in payloads
        if endpoint == "/api/v1/query" and not payload.get("response_format")
    )
    system_message = next(message["content"] for message in query_payload["messages"] if message["role"] == "system")
    assert "Session summary:" in system_message
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
    assert query_attempts["count"] >= 2
    assert len(system_lengths) >= 2
    assert min(system_lengths[1:]) < system_lengths[0]
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
    assert isinstance(result.limitations, list)


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
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 2,
                                    "actual_rows": 2,
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
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 2,
                                    "actual_rows": 2,
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
        if endpoint_name == "/api/v1/query/validator":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expected_rows": 2,
                                    "actual_rows": 2,
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
        if endpoint_name == "/api/v1/query/replan":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"should_replan": False, "reason": "test stub", "steps": []}
                            )
                        }
                    }
                ]
            }
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
    assert any(trace.tool in {"stock.extract_variant_evidence", "stock.get_product"} for trace in result.tool_trace)
    assert any("runtime executed planned step `3` directly" in item for item in result.limitations)
    assert any("Runtime recovered missing lookup args for planned step `3`" in item for item in result.limitations)
    assert not any("Invalid arguments for 'stock.extract_variant_evidence'" in item for item in result.limitations)


@pytest.mark.anyio
async def test_resolve_or_insert_binds_get_product_rewrite_to_pending_variant_evidence_step() -> None:
    container = await build_container(build_engine_settings())
    try:
        plan = PlanStatus(
            goal="test",
            intent_classes=["stock"],
            steps=[
                PlanStep(
                    id=1,
                    name="search",
                    tool="stock.search_catalogue",
                    status="done",
                    args={"search": "Spencer", "page": 1, "pageSize": 5},
                ),
                PlanStep(
                    id=2,
                    name="variant evidence",
                    tool="stock.extract_variant_evidence",
                    status="pending",
                    args={"id": "sp-p1"},
                ),
            ],
        )
        gp_args = {"id": "sp-p1", "page": 1, "pageSize": 20}
        step, inserted = container.agent_engine._resolve_or_insert_plan_step(
            plan,
            "stock.get_product",
            gp_args,
            binding_source_tool="stock.extract_variant_evidence",
        )
        assert inserted is False
        assert step.id == 2
        assert step.args["id"] == "sp-p1"
        assert step.args.get("pageSize") == 20
    finally:
        await container.close()


@pytest.mark.anyio
async def test_resolve_or_insert_reuses_done_step_for_identical_runtime_call() -> None:
    container = await build_container(build_engine_settings())
    try:
        plan = PlanStatus(
            goal="test duplicate runtime call dedupe",
            intent_classes=["stock"],
            steps=[
                PlanStep(
                    id=1,
                    name="catalogue search",
                    tool="stock.search_catalogue",
                    status="done",
                    args={"search": "Spencer chair", "page": 1, "pageSize": 10, "departmentId": 3},
                )
            ],
        )
        step, inserted = container.agent_engine._resolve_or_insert_plan_step(
            plan,
            "stock.search_catalogue",
            {"search": "Spencer chair", "page": 1, "pageSize": 10, "departmentId": 3},
        )
        assert inserted is False
        assert step.id == 1
        assert step.status == "done"
        assert len(plan.steps) == 1
    finally:
        await container.close()


@pytest.mark.anyio
async def test_resolve_or_insert_reuses_semantically_equivalent_catalogue_search_step() -> None:
    container = await build_container(build_engine_settings())
    try:
        plan = PlanStatus(
            goal="test semantic catalogue dedupe",
            intent_classes=["stock"],
            steps=[
                PlanStep(
                    id=1,
                    name="catalogue search",
                    tool="stock.search_catalogue",
                    status="done",
                    args={"search": "Spencer chair", "page": 1, "pageSize": 10, "departmentId": 3},
                )
            ],
        )
        step, inserted = container.agent_engine._resolve_or_insert_plan_step(
            plan,
            "stock.search_catalogue",
            {"search": "chair Spencer", "page": 1, "pageSize": 10, "departmentId": 3},
        )
        assert inserted is False
        assert step.id == 1
        assert step.status == "done"
        assert len(plan.steps) == 1
    finally:
        await container.close()


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
async def test_agent_engine_continues_with_get_product_after_resolved_product_family() -> None:
    container = await build_container(build_engine_settings())

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Resolve product details",
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "disambiguate family",
                                            "tool": "resolver.disambiguate_candidates",
                                            "status": "planned",
                                            "args": {"query": "alto chair", "limit": 10},
                                            "depends_on": [],
                                            "parallel_group": None,
                                            "hypotheses": ["Resolve product family first."],
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
                                    "confidence": 0.92,
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
                            "content": "Here are all resolved Alto Chair variants. If you want, I can go deeper on one variant."
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
                                        "answer": "Here are all resolved Alto Chair variants.",
                                        "limitations": [],
                                        "clarification": None,
                                    }
                                )
                            }
                        }
                    ]
                }
            # First assistant turn calls resolver. Second turn returns no tool calls
            # so runtime auto-executes pending follow-up step.
            tool_messages = [message for message in payload["messages"] if message.get("role") == "tool"]
            if not tool_messages:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "<thought>goal: disambiguate</thought>",
                                "tool_calls": [
                                    {
                                        "id": "call-resolver",
                                        "type": "function",
                                        "function": {
                                            "name": "resolver.disambiguate_candidates",
                                            "arguments": json.dumps({"query": "alto chair", "limit": 10}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "Proceed with resolved product details."}}]}
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    async def fake_call_tool(tool_name, raw_args, session_id=None, thought=""):  # noqa: ANN001
        if tool_name == "resolver.disambiguate_candidates":
            return ToolResult(
                tool=tool_name,
                data={
                    "status": "resolved_product_family",
                    "query": "alto chair",
                    "product_id": "prod-alto",
                    "product_name": "Alto Chair",
                    "variant_count": 6,
                    "candidate_count": 6,
                },
                trace=ToolTrace(
                    thought=thought,
                    tool=tool_name,
                    args=raw_args,
                    status="ok",
                    result_count=1,
                ),
            )
        if tool_name == "stock.get_product":
            return ToolResult(
                tool=tool_name,
                data={
                    "items": [
                        {
                            "id": "prod-alto",
                            "name": "Alto Chair",
                            "variants": [
                                {"id": "var-1", "name": "Alto Chair - Black", "sku": "fn-se-ch-alt-bla"},
                                {"id": "var-2", "name": "Alto Chair - White", "sku": "fn-se-ch-alt-whi"},
                            ],
                        }
                    ],
                    "page": 1,
                    "pageSize": 50,
                    "totalCount": 1,
                    "totalPages": 1,
                },
                llm_content={"items": [{"id": "prod-alto", "name": "Alto Chair"}]},
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
        session_state, _ = await container.session_store.get_state("engine-resolved-family")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="let me know all details about an alto chair",
                sessionId="engine-resolved-family",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert "variants" in result.answer.lower()
    assert not result.clarification
    assert any(trace.tool == "resolver.disambiguate_candidates" for trace in result.tool_trace)
    assert any(trace.tool == "stock.get_product" for trace in result.tool_trace)


@pytest.mark.anyio
async def test_agent_engine_prunes_overscheduled_compare_after_snapshot() -> None:
    container = await build_container(build_engine_settings())

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Summarize the product family",
                                    "intent_classes": ["stock"],
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "inventory snapshot",
                                            "tool": "stock.inventory_snapshot",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 10, "search": "alto chair", "departmentId": 3},
                                            "depends_on": [],
                                            "parallel_group": 1,
                                            "hypotheses": ["Retrieve the family in one compact payload first."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 2,
                                            "name": "compare family variants",
                                            "tool": "stock.compare_variants",
                                            "status": "planned",
                                            "args": {
                                                "identifiers": [
                                                    "fn-se-ch-alt-bla",
                                                    "fn-se-ch-alt-blu",
                                                    "fn-se-ch-alt-sag",
                                                    "fn-se-ch-alt-sof",
                                                    "fn-se-ch-alt-ste",
                                                    "fn-se-ch-alt-whi",
                                                ]
                                            },
                                            "depends_on": [],
                                            "parallel_group": 1,
                                            "hypotheses": ["Compare every resolved variant side by side."],
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
                                    "expected_rows": 2,
                                    "actual_rows": 2,
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
        if endpoint_name == "/api/v1/query":
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<thought>\n"
                                "goal: fetch the family and compare it\n"
                                "entity_guess: product\n"
                                "strategy: retrieval then comparison\n"
                                "tool: multi\n"
                                "args_draft: {}\n"
                                "risk: none\n"
                                "</thought>"
                            ),
                            "tool_calls": [
                                {
                                    "id": "call_snapshot",
                                    "type": "function",
                                    "function": {
                                        "name": "stock.inventory_snapshot",
                                        "arguments": json.dumps({"page": 1, "pageSize": 10, "search": "alto chair", "departmentId": 3}),
                                    },
                                },
                                {
                                    "id": "call_compare",
                                    "type": "function",
                                    "function": {
                                        "name": "stock.compare_variants",
                                        "arguments": json.dumps(
                                            {
                                                "identifiers": [
                                                    "fn-se-ch-alt-bla",
                                                    "fn-se-ch-alt-blu",
                                                    "fn-se-ch-alt-sag",
                                                    "fn-se-ch-alt-sof",
                                                    "fn-se-ch-alt-ste",
                                                    "fn-se-ch-alt-whi",
                                                ]
                                            }
                                        ),
                                    },
                                },
                            ],
                        }
                    }
                ]
            }
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    async def fake_call_tool(tool_name, raw_args, session_id=None, thought=""):  # noqa: ANN001
        if tool_name == "stock.compare_variants":
            raise AssertionError("stock.compare_variants should be pruned once snapshot evidence exists")
        if tool_name == "stock.inventory_snapshot":
            variant_specs = [
                ("Black", "fn-se-ch-alt-bla", 172, 120, 50.0),
                ("Blue", "fn-se-ch-alt-blu", 96, 70, 48.0),
                ("Sage", "fn-se-ch-alt-sag", 64, 48, 47.0),
                ("Soft", "fn-se-ch-alt-sof", 88, 61, 49.0),
                ("Steel", "fn-se-ch-alt-ste", 105, 75, 51.0),
                ("White", "fn-se-ch-alt-whi", 232, 180, 41.0),
            ]
            snapshot_data = {
                "rows": [
                    {
                        "product": "Alto Chair",
                        "variant": f"Alto Chair - {color}",
                        "sku": sku,
                        "attributeEvidence": [f"Alto Chair - {color}", color],
                        "size": "0.5 x 0.5 x 0.9 m",
                        "stock": f"Overall has {total_stock} in stock. By location: VIC has {vic_stock} in stock.",
                        "knownSpecs": [f"cost={cost:g}", "generalRate=90"],
                    }
                    for color, sku, total_stock, vic_stock, cost in variant_specs
                ],
                "evidence": [
                    {
                        "product_id": "prod-alto",
                        "product_name": "Alto Chair",
                        "variant_id": f"var-{color.lower()}",
                        "variant_name": f"Alto Chair - {color}",
                        "sku": sku,
                        "variation_options": [color],
                        "salesNote": None,
                        "departmentId": 3,
                        "subDepartmentId": None,
                        "categoryId": "cat-chair",
                        "isActive": True,
                        "pricing": {"generalRate": 90.0, "expoRate": 90.0, "cost": cost},
                        "dimensions": {"dimensional": True, "canBeSoldInPortions": False, "length": 0.5, "width": 0.5, "height": 0.9},
                        "stock": {"totalHirable": total_stock - 10, "vicStock": vic_stock, "vicHirable": max(vic_stock - 8, 0), "nswStock": 20, "nswHirable": 18, "qldStock": 12, "qldHirable": 10, "totalStock": total_stock},
                        "lifecycle": {"isActive": True, "startDate": None, "endDate": None, "lastUpdatedDate": None},
                        "media": {"imageFileName": None, "imageUrl": None},
                        "components": [],
                        "provenance": {"tool": "stock.inventory_snapshot", "matched_on": ["catalogue_snapshot"], "confidence": 0.96, "source_path": "items[0].variants[0].details"},
                        "evidence_paths": {},
                    }
                    for color, sku, total_stock, vic_stock, cost in variant_specs
                ],
                "coverage": {
                    "requestedPage": 1,
                    "requestedPageSize": 10,
                    "matchedProducts": 1,
                    "matchedPages": 1,
                    "enrichedProducts": 1,
                    "enrichedVariants": 6,
                    "isPartial": False,
                    "limitations": [],
                },
            }
            return ToolResult(
                tool=tool_name,
                data=snapshot_data,
                llm_content=snapshot_data,
                trace=ToolTrace(
                    thought=thought,
                    tool=tool_name,
                    args=raw_args,
                    status="ok",
                    result_count=6,
                ),
            )
        raise AssertionError(f"Unexpected tool call: {tool_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]
    container.tool_registry.call_tool = fake_call_tool  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-pruned-compare")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message="Let me know all details about an Alto chair.",
                sessionId="engine-pruned-compare",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert "Alto Chair - Black" in result.answer
    assert "Alto Chair - White" in result.answer
    assert not any(trace.tool == "stock.compare_variants" for trace in result.tool_trace)
    assert any("Pruned planned step" in item for item in result.limitations)


@pytest.mark.anyio
async def test_agent_engine_uses_composer_for_mixed_query_even_with_snapshot() -> None:
    container = await build_container(build_engine_settings())

    async def fake_post_chat_completion(payload, endpoint_name):  # noqa: ANN001
        if endpoint_name == "/api/v1/query/planner":
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "goal": "Answer the furniture research request",
                                    "intent_classes": ["stock", "currency", "news"],
                                    "steps": [
                                        {
                                            "id": 1,
                                            "name": "inventory snapshot",
                                            "tool": "stock.inventory_snapshot",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 10, "search": "alto chair", "departmentId": 3},
                                            "depends_on": [],
                                            "parallel_group": 1,
                                            "hypotheses": ["Retrieve all chair variants first."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 2,
                                            "name": "redundant compare",
                                            "tool": "stock.compare_variants",
                                            "status": "planned",
                                            "args": {"identifiers": ["fn-se-ch-alt-bla", "fn-se-ch-alt-whi"]},
                                            "depends_on": [],
                                            "parallel_group": 1,
                                            "hypotheses": ["Compare the same variants again."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 3,
                                            "name": "convert currency",
                                            "tool": "currency.convert",
                                            "status": "planned",
                                            "args": {"from": "AUD", "to": "USD", "amount": 90.0},
                                            "depends_on": [1],
                                            "parallel_group": 2,
                                            "hypotheses": ["Convert the chair cost for comparison."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 4,
                                            "name": "melbourne news",
                                            "tool": "news.search",
                                            "status": "planned",
                                            "args": {"q": "Melbourne events business hospitality demand", "pageSize": 3},
                                            "depends_on": [],
                                            "parallel_group": 2,
                                            "hypotheses": ["Assess local demand signals."],
                                            "validation": None,
                                        },
                                        {
                                            "id": 5,
                                            "name": "table search",
                                            "tool": "stock.search_catalogue",
                                            "status": "planned",
                                            "args": {"page": 1, "pageSize": 5, "search": "table", "departmentId": 3},
                                            "depends_on": [],
                                            "parallel_group": 2,
                                            "hypotheses": ["Find matching tables."],
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
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Alto Chair is available in black and white, costs 90 AUD and about 58.5 USD, "
                                "Melbourne headlines show active hospitality demand, and the retrieved table search "
                                "returned matching table options."
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
                                    "answer": (
                                        "Alto Chair is available in black and white, costs 90 AUD and about 58.5 USD, "
                                        "Melbourne headlines show active hospitality demand, and the retrieved table "
                                        "search returned matching table options."
                                    ),
                                    "limitations": [],
                                    "clarification": None,
                                }
                            )
                        }
                    }
                ]
            }
        if endpoint_name == "/api/v1/query":
            tool_messages = [message for message in payload["messages"] if message.get("role") == "tool"]
            if not tool_messages:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "<thought>goal: satisfy all requested domains</thought>",
                                "tool_calls": [
                                    {
                                        "id": "call_snapshot",
                                        "type": "function",
                                        "function": {
                                            "name": "stock.inventory_snapshot",
                                            "arguments": json.dumps({"page": 1, "pageSize": 10, "search": "alto chair", "departmentId": 3}),
                                        },
                                    },
                                    {
                                        "id": "call_compare",
                                        "type": "function",
                                        "function": {
                                            "name": "stock.compare_variants",
                                            "arguments": json.dumps({"identifiers": ["fn-se-ch-alt-bla", "fn-se-ch-alt-whi"]}),
                                        },
                                    },
                                    {
                                        "id": "call_currency",
                                        "type": "function",
                                        "function": {
                                            "name": "currency.convert",
                                            "arguments": json.dumps({"from": "AUD", "to": "USD", "amount": 90.0}),
                                        },
                                    },
                                    {
                                        "id": "call_news",
                                        "type": "function",
                                        "function": {
                                            "name": "news.search",
                                            "arguments": json.dumps({"q": "Melbourne events business hospitality demand", "pageSize": 3}),
                                        },
                                    },
                                    {
                                        "id": "call_tables",
                                        "type": "function",
                                        "function": {
                                            "name": "stock.search_catalogue",
                                            "arguments": json.dumps({"page": 1, "pageSize": 5, "search": "table", "departmentId": 3}),
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "Use the grounded evidence and finish the mixed-domain answer."}}]}
        raise AssertionError(f"Unexpected endpoint call: {endpoint_name}")

    async def fake_call_tool(tool_name, raw_args, session_id=None, thought=""):  # noqa: ANN001
        if tool_name == "stock.compare_variants":
            raise AssertionError("stock.compare_variants should be pruned for the mixed-domain query too")
        if tool_name == "stock.inventory_snapshot":
            snapshot_data = {
                "rows": [
                    {
                        "product": "Alto Chair",
                        "variant": "Alto Chair - Black",
                        "sku": "fn-se-ch-alt-bla",
                        "attributeEvidence": ["Alto Chair - Black", "Black"],
                        "size": "0.5 x 0.5 x 0.9 m",
                        "stock": "Overall has 172 in stock. By location: VIC has 120 in stock.",
                        "knownSpecs": ["cost=90", "generalRate=90"],
                    },
                    {
                        "product": "Alto Chair",
                        "variant": "Alto Chair - White",
                        "sku": "fn-se-ch-alt-whi",
                        "attributeEvidence": ["Alto Chair - White", "White"],
                        "size": "0.5 x 0.5 x 0.9 m",
                        "stock": "Overall has 232 in stock. By location: VIC has 180 in stock.",
                        "knownSpecs": ["cost=90", "generalRate=90"],
                    },
                ],
                "evidence": [
                    {
                        "product_id": "prod-alto",
                        "product_name": "Alto Chair",
                        "variant_id": "var-black",
                        "variant_name": "Alto Chair - Black",
                        "sku": "fn-se-ch-alt-bla",
                        "variation_options": ["Black"],
                        "salesNote": None,
                        "departmentId": 3,
                        "subDepartmentId": None,
                        "categoryId": "cat-chair",
                        "isActive": True,
                        "pricing": {"generalRate": 90.0, "expoRate": 90.0, "cost": 90.0},
                        "dimensions": {"dimensional": True, "canBeSoldInPortions": False, "length": 0.5, "width": 0.5, "height": 0.9},
                        "stock": {"totalHirable": 160, "vicStock": 120, "vicHirable": 110, "nswStock": 40, "nswHirable": 38, "qldStock": 12, "qldHirable": 12, "totalStock": 172},
                        "lifecycle": {"isActive": True, "startDate": None, "endDate": None, "lastUpdatedDate": None},
                        "media": {"imageFileName": None, "imageUrl": None},
                            "components": [],
                            "provenance": {"tool": "stock.inventory_snapshot", "matched_on": ["catalogue_snapshot"], "confidence": 0.96, "source_path": "items[0].variants[0].details"},
                            "evidence_paths": {},
                        },
                        {
                            "product_id": "prod-alto",
                            "product_name": "Alto Chair",
                            "variant_id": "var-white",
                            "variant_name": "Alto Chair - White",
                            "sku": "fn-se-ch-alt-whi",
                            "variation_options": ["White"],
                            "salesNote": None,
                            "departmentId": 3,
                            "subDepartmentId": None,
                            "categoryId": "cat-chair",
                            "isActive": True,
                            "pricing": {"generalRate": 90.0, "expoRate": 90.0, "cost": 90.0},
                            "dimensions": {"dimensional": True, "canBeSoldInPortions": False, "length": 0.5, "width": 0.5, "height": 0.9},
                            "stock": {"totalHirable": 220, "vicStock": 180, "vicHirable": 170, "nswStock": 38, "nswHirable": 35, "qldStock": 14, "qldHirable": 12, "totalStock": 232},
                            "lifecycle": {"isActive": True, "startDate": None, "endDate": None, "lastUpdatedDate": None},
                            "media": {"imageFileName": None, "imageUrl": None},
                            "components": [],
                            "provenance": {"tool": "stock.inventory_snapshot", "matched_on": ["catalogue_snapshot"], "confidence": 0.96, "source_path": "items[0].variants[1].details"},
                            "evidence_paths": {},
                        },
                    ],
                "coverage": {
                    "requestedPage": 1,
                    "requestedPageSize": 10,
                    "matchedProducts": 1,
                    "matchedPages": 1,
                    "enrichedProducts": 1,
                    "enrichedVariants": 2,
                    "isPartial": False,
                    "limitations": [],
                },
            }
            return ToolResult(
                tool=tool_name,
                data=snapshot_data,
                llm_content=snapshot_data,
                trace=ToolTrace(thought=thought, tool=tool_name, args=raw_args, status="ok", result_count=2),
            )
        if tool_name == "currency.convert":
            return ToolResult(
                tool=tool_name,
                data={"query": {"from": "AUD", "to": "USD", "amount": 90.0}, "result": 58.5, "info": {"rate": 0.65}},
                trace=ToolTrace(thought=thought, tool=tool_name, args=raw_args, status="ok", result_count=1),
            )
        if tool_name == "news.search":
            return ToolResult(
                tool=tool_name,
                data={"topSources": ["The Age"], "topKeywords": ["Melbourne", "events"], "publishedRange": {"from": "2026-04-20", "to": "2026-04-24"}, "totalResults": 3, "matchingArticles": [{"title": "Melbourne hospitality demand rises", "matchingKeywords": ["Melbourne", "hospitality"]}]},
                trace=ToolTrace(thought=thought, tool=tool_name, args=raw_args, status="ok", result_count=3),
            )
        if tool_name == "stock.search_catalogue":
            return ToolResult(
                tool=tool_name,
                data={"items": [{"id": "table-1", "name": "Arc Side Table", "variants": [{"id": "var-table-1", "name": "Arc Side Table - Oak", "sku": "tb-arc-side-oak"}]}], "page": 1, "pageSize": 5, "totalCount": 1, "totalPages": 1},
                llm_content={"items": [{"id": "table-1", "name": "Arc Side Table", "variants": [{"id": "var-table-1", "name": "Arc Side Table - Oak", "sku": "tb-arc-side-oak"}]}]},
                trace=ToolTrace(thought=thought, tool=tool_name, args=raw_args, status="ok", result_count=1),
            )
        if tool_name == "stock.get_product":
            return ToolResult(
                tool=tool_name,
                data={
                    "items": [
                        {
                            "id": "table-1",
                            "name": "Arc Side Table",
                            "variants": [{"id": "var-table-1", "name": "Arc Side Table - Oak", "sku": "tb-arc-side-oak"}],
                        }
                    ],
                    "page": 1,
                    "pageSize": 50,
                    "totalCount": 1,
                    "totalPages": 1,
                },
                llm_content={"items": [{"id": "table-1", "name": "Arc Side Table"}]},
                trace=ToolTrace(thought=thought, tool=tool_name, args=raw_args, status="ok", result_count=1),
            )
        raise AssertionError(f"Unexpected tool call: {tool_name}")

    container.agent_engine._post_chat_completion = fake_post_chat_completion  # type: ignore[method-assign]
    container.tool_registry.call_tool = fake_call_tool  # type: ignore[method-assign]

    try:
        session_state, _ = await container.session_store.get_state("engine-mixed-query")
        result = await container.agent_engine.run(
            AgentQueryRequest(
                message=(
                    "Let me know the size, colours, stock in Victoria, cost in AUD and USD, compare it, "
                    "check Melbourne news for demand signals, and suggest matching tables."
                ),
                sessionId="engine-mixed-query",
                includeThoughts=False,
            ),
            session_state,
        )
    finally:
        await container.close()

    assert result.status == "answered"
    assert "58.5 USD" in result.answer
    assert "Melbourne headlines" in result.answer
    assert "table options" in result.answer
    assert not result.clarification
    assert not any(trace.tool == "stock.compare_variants" for trace in result.tool_trace)
    assert {trace.tool for trace in result.tool_trace} == {
        "stock.inventory_snapshot",
        "currency.convert",
        "news.search",
        "stock.search_catalogue",
        "stock.get_product",
    }


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
    assert any(trace.tool in {"stock.extract_variant_evidence", "stock.get_product"} for trace in result.tool_trace)
    assert len(result.plan_status.steps) >= 2
    assert result.plan_status.steps[1].depends_on == [1]
    assert any("recursive detail retrieval step(s)" in item for item in result.limitations)
    assert result.debug is not None


@pytest.mark.anyio
async def test_agent_engine_derives_variant_follow_up_steps_for_all_unique_variants() -> None:
    container = await build_container(build_engine_settings())
    try:
        steps = container.agent_engine._derive_follow_up_steps(
            data={
                "items": [
                    {
                        "name": "Alto Chair",
                        "variants": [
                            {"id": "var-1", "sku": "alto-black"},
                            {"id": "var-2", "sku": "alto-white"},
                            {"id": "var-2", "sku": "alto-white"},
                        ],
                    }
                ]
            },
            request_message="alto chair sizes and stock",
        )
    finally:
        await container.close()

    assert len(steps) == 2
    assert all(tool_name == "stock.extract_variant_evidence" for tool_name, _ in steps)
    assert {args.get("sku") for _, args in steps} == {"alto-black", "alto-white"}
