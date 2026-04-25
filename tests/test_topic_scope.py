from __future__ import annotations

import pytest

from app.config import Settings, build_container
from app.schemas import (
    ActiveSubjectSnapshot,
    MemoCache,
    MemoEntry,
    PlanStatus,
    PlanStep,
    SessionMemoryScope,
    SessionState,
)
from app.session.topic_scope import apply_virtual_pruning, derive_memory_scope
from app.prompt.context import render_session_context


def build_engine_settings() -> Settings:
    return Settings(
        local_harmonise=True,
        log_level="warning",
        mock_catalog_path="./mock/product-catalog.json",
        mock_details_path="./mock/product-details.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        redis_url="redis://127.0.0.1:65535",
        enable_mock_ui_simulation=False,
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
    )


def test_topic_scope_detects_topic_shift_for_distinct_entity() -> None:
    session = SessionState(
        session_id="scope-shift",
        active_subject=ActiveSubjectSnapshot(
            label="Alto Chair",
            product_names=["Alto Chair"],
            identifiers=["fn-se-ch-alt-bla"],
            source="evidence",
        ),
        recent_product_names=["Alto Chair"],
        recent_resolved_identifiers=["fn-se-ch-alt-bla"],
    )
    scope = derive_memory_scope("Tell me about Spencer chair dimensions", session)
    assert scope.transition == "topic_shift"
    assert scope.allow_background_reference is False


def test_topic_scope_detects_continuation_for_anaphora_follow_up() -> None:
    session = SessionState(
        session_id="scope-follow-up",
        active_subject=ActiveSubjectSnapshot(label="Spencer Chair", product_names=["Spencer Chair"]),
    )
    scope = derive_memory_scope("What about its stock?", session)
    assert scope.transition == "continuation"
    assert "anaphora" in scope.bridge_signals


def test_topic_scope_allows_background_for_comparison_requests() -> None:
    session = SessionState(
        session_id="scope-compare",
        active_subject=ActiveSubjectSnapshot(label="Spencer Chair", product_names=["Spencer Chair"]),
    )
    scope = derive_memory_scope("Compare that with Baxter chair", session)
    assert scope.transition == "continuation"
    assert scope.allow_background_reference is True
    assert "comparative" in scope.bridge_signals


def test_apply_virtual_pruning_resets_entity_memory_and_archives_subject() -> None:
    session = SessionState(
        session_id="scope-prune",
        active_subject=ActiveSubjectSnapshot(
            label="Alto Chair",
            product_names=["Alto Chair"],
            identifiers=["fn-se-ch-alt-bla"],
            source="evidence",
        ),
        recent_product_names=["Alto Chair"],
        recent_resolved_identifiers=["fn-se-ch-alt-bla"],
        memo_cache=MemoCache(entries=[MemoEntry(tool="stock.get_product", rows=[{"product": "Alto Chair"}])]),
        conversation_history=[],
    )
    scope = SessionMemoryScope(transition="topic_shift", target_entity="Spencer Chair")
    apply_virtual_pruning(session, scope)

    assert session.recent_product_names == []
    assert session.recent_resolved_identifiers == []
    assert session.memo_cache.entries == []
    assert session.background_subjects
    assert session.background_subjects[0].label == "Alto Chair"
    assert session.memory_scope.transition == "topic_shift"


def test_render_session_context_scopes_memo_to_active_subject_on_topic_shift() -> None:
    session = SessionState(
        session_id="scope-render",
        active_subject=ActiveSubjectSnapshot(label="Spencer Chair", product_names=["Spencer Chair"]),
        memory_scope=SessionMemoryScope(transition="topic_shift", target_entity="Spencer Chair"),
        memo_cache=MemoCache(
            entries=[
                MemoEntry(
                    tool="stock.get_product",
                    args={"search": "Alto Chair"},
                    rows=[{"product": "Alto Chair", "sku": "fn-se-ch-alt-bla"}],
                    provenance={"subject_names": ["Alto Chair"]},
                ),
                MemoEntry(
                    tool="stock.get_product",
                    args={"search": "Spencer Chair"},
                    rows=[{"product": "Spencer Chair", "sku": "fn-se-ch-spe-bla"}],
                    provenance={"subject_names": ["Spencer Chair"]},
                ),
            ]
        ),
    )
    rendered = render_session_context(session, "Tell me more about Spencer chair", mode="compact")
    assert "Spencer Chair" in rendered
    assert "Alto Chair" not in rendered


@pytest.mark.anyio
async def test_plan_query_does_not_resume_in_progress_plan_on_topic_shift() -> None:
    container = await build_container(build_engine_settings())
    try:
        session = SessionState(
            session_id="scope-plan-shift",
            current_plan=PlanStatus(
                goal="Old Alto plan",
                steps=[PlanStep(id=1, name="old", tool="stock.get_product", status="in-progress")],
                status="in-progress",
            ),
            memory_scope=SessionMemoryScope(transition="topic_shift", target_entity="Spencer Chair"),
        )
        container.agent_engine._client = None
        plan, _ = await container.agent_engine.plan_query("New Spencer request", session)
        assert plan.goal == "New Spencer request"
    finally:
        await container.close()


@pytest.mark.anyio
async def test_plan_query_resumes_in_progress_plan_on_continuation() -> None:
    container = await build_container(build_engine_settings())
    try:
        session = SessionState(
            session_id="scope-plan-continue",
            current_plan=PlanStatus(
                goal="Existing Spencer plan",
                steps=[PlanStep(id=1, name="existing", tool="stock.get_product", status="in-progress")],
                status="in-progress",
            ),
            memory_scope=SessionMemoryScope(transition="continuation"),
        )
        container.agent_engine._client = None
        plan, _ = await container.agent_engine.plan_query("its size", session)
        assert plan.goal == "Existing Spencer plan"
    finally:
        await container.close()
