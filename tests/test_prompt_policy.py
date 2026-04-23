from __future__ import annotations

from app.prompt.context import render_session_context
from app.prompt.registry import render_formatter, render_system
from app.schemas import ConversationTurn, MemoCache, MemoEntry, PlanStatus, PlanStep, SessionState, ToolDefinition


def test_render_system_includes_scope_and_variant_policy_guards() -> None:
    prompt = render_system(
        request="What is the height of Alto Chair?",
        session=SessionState(session_id="policy-test"),
        tools=[],
    )
    assert "Keep answers scoped to the requested attributes" in prompt
    assert "If the user targets a specific variant directly, answer that variant only." in prompt
    assert "Prefer product and variant names over SKU" in prompt
    assert "Final answer wording must align with the original user request intent" in prompt


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

    assert "Session memory summary:" in prompt
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
