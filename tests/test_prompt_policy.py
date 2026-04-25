from __future__ import annotations

from app.prompt.context import render_session_context
from app.prompt.registry import render_formatter, render_planner, render_system
from app.schemas import ConversationTurn, MemoCache, MemoEntry, PlanStatus, PlanStep, SessionState, ToolDefinition


def test_render_system_includes_scope_and_variant_policy_guards() -> None:
    prompt = render_system(
        request="What is the height of Alto Chair?",
        session=SessionState(session_id="policy-test"),
        tools=[],
    )
    assert "Role: Harmonise Orchestrator" in prompt
    assert "Handle each request as recursive discovery" in prompt
    assert "Build tool args from schema + retrieved evidence" in prompt
    assert "Keep answers scoped to requested attributes only." in prompt
    assert "If user targets a specific variant, answer that variant only." in prompt
    assert "Prefer product + variant names over SKU" in prompt
    assert "Avoid duplicate semantic retrieval" in prompt
    assert "reasonable latency budget" in prompt
    assert "Final wording must match original user intent." in prompt
    assert "Do not reveal hidden chain-of-thought" in prompt


def test_formatter_contract_requires_intent_aligned_scope() -> None:
    formatter_prompt = render_formatter(
        request="Show the image for Alto Chair - Black",
        draft="Here is the image.",
        limitations=[],
        clarification=None,
    )
    assert "Keep the answer scoped to the user's requested attributes" in formatter_prompt
    assert "answer only that variant" in formatter_prompt
    assert "deduplicate repeated values" in formatter_prompt
    assert "aligned to the user's original intent" in formatter_prompt
    assert "not yet implemented in the current tool contract" in formatter_prompt


def test_render_planner_requires_dag_metadata_and_stock_only_contract() -> None:
    planner_prompt = render_planner(
        request="Compare two floor variants and show stock.",
        session=SessionState(session_id="planner-policy"),
        tools=[
            ToolDefinition(
                name="stock.search_catalogue",
                description="Search the catalogue.",
                input_schema={"type": "object", "properties": {"page": {}, "pageSize": {}, "search": {}}},
            ),
            ToolDefinition(
                name="stock.get_product",
                description="Get product detail.",
                input_schema={"type": "object", "properties": {"sku": {}, "id": {}}},
            ),
        ],
    )

    assert "- depends_on: array of earlier step ids" in planner_prompt
    assert "- parallel_group: integer or null" in planner_prompt
    assert "may be empty only for static capability answers" in planner_prompt
    assert "intent_classes: [\"capability\"]" in planner_prompt
    assert "FURNITURE_CAPABILITY_SUMMARY" not in planner_prompt
    assert "supported_department_count" in planner_prompt
    assert "mapped_furniture_category_count" in planner_prompt
    assert "follow-up retrieval step" in planner_prompt
    assert "avoid planning both `stock.inventory_snapshot` and `stock.compare_variants`" in planner_prompt
    assert "latency-aware" in planner_prompt
    assert "not yet implemented in the current tool contract" in planner_prompt


def test_render_system_uses_compact_memory_and_tool_roster() -> None:
    session = SessionState(
        session_id="compact-policy",
        recent_product_names=["Dance Floor - White Gloss", "Armour Floor - Black"],
        current_plan=PlanStatus(
            goal="List inventory",
            steps=[
                PlanStep(
                    id=1,
                    name="search catalogue",
                    tool="stock.search_catalogue",
                    status="in-progress",
                    args={"page": 1, "pageSize": 20, "search": "floor"},
                    hypotheses=["Search the catalogue first."],
                )
            ],
            memo=MemoCache(
                entries=[
                    MemoEntry(
                        step_id=1,
                        tool="stock.search_catalogue",
                        args={"page": 1, "pageSize": 20, "search": "floor"},
                        rows=[
                            {
                                "product": "Dance Floor - White Gloss",
                                "variant": "Dance Floor - White Gloss",
                                "sku": "fl-da-dan",
                                "size": "2 x 2 m",
                                "stock": "Overall: 6 in stock, 4 available to hire",
                                "knownSpecs": ["gloss finish", "sales note=white gloss"],
                            }
                        ],
                        evidence=[],
                        aggregates={},
                        provenance={},
                    )
                ],
                aggregates={},
            ),
            status="in-progress",
        ),
    )
    prompt = render_system(
        request="What floor options do we have?",
        session=session,
        tools=[
            ToolDefinition(
                name="stock.search_catalogue",
                description="Search the catalogue.",
                input_schema={"type": "object", "properties": {"page": {}, "pageSize": {}, "search": {}}},
            )
        ],
        context_mode="compact",
    )

    assert "Session summary:" in prompt
    assert "memo_cache" not in prompt
    assert "conversation_history" not in prompt
    assert "\"input_schema\"" not in prompt
    assert "- stock.search_catalogue: Search the catalogue." in prompt


def test_render_session_context_chunks_tables_without_breaking_headers() -> None:
    session = SessionState(
        session_id="table-chunking",
        conversation_history=[
            ConversationTurn(
                role="assistant",
                content=(
                    "| Product | Variant | SKU | Size | Stock |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    + "\n".join(
                        f"| Floor {index} | Variant {index} | sku-{index} | 1 x 1 m | {index} in stock |"
                        for index in range(8)
                    )
                ),
            )
        ],
    )

    summary = render_session_context(session, "Which floor items are in stock?", mode="compact")

    assert summary.count("table chunk ") == 2
    assert summary.count("| Product | Variant | SKU | Size | Stock |") == 2
    assert "| Floor 0 | Variant 0 | sku-0 | 1 x 1 m | 0 in stock |" in summary
    assert "| Floor 7 | Variant 7 | sku-7 | 1 x 1 m | 7 in stock |" in summary


def test_render_system_routes_furniture_department_and_category_mapping() -> None:
    prompt = render_system(
        request="Show me chairs and lounges in stock.",
        session=SessionState(session_id="furniture-routing"),
        tools=[],
    )

    assert "departmentId=3" in prompt
    assert "FURNITURE_CAPABILITY_SUMMARY" in prompt
    assert "mapped_furniture_category_count" in prompt
    assert "b7d70000-eacf-fc4c-c59a-08de7f19d85e" in prompt
    assert "b7d70000-eacf-fc4c-359b-08de7f19d91e" in prompt


def test_render_system_handles_mixed_furniture_and_unsupported_departments() -> None:
    prompt = render_system(
        request="Show me stools and electronics inventory.",
        session=SessionState(session_id="mixed-department"),
        tools=[],
    )

    assert "electronics is unavailable" in prompt
    assert "only Furniture is supported right now" in prompt


def test_render_system_plugin_only_query_avoids_furniture_mapping_rules() -> None:
    prompt = render_system(
        request="What's the weather forecast for Melbourne tomorrow?",
        session=SessionState(session_id="plugin-only"),
        tools=[],
        intent_classes=["weather"],
    )

    assert "WEATHER Example:" in prompt
    assert "categoryId=b7d70000-eacf-fc4c-c59a-08de7f19d85e" not in prompt
