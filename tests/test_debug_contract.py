from __future__ import annotations

from app.schemas import (
    AgentDebugGrounding,
    AgentDebugIntent,
    AgentDebugPayload,
    AgentDebugPlan,
    AgentDebugPlanStep,
    AgentDebugRetrieval,
    AgentQueryResponse,
    PlanStep,
    SessionState,
)


def test_plan_step_supports_dag_metadata() -> None:
    step = PlanStep(
        id=2,
        name="follow-up detail lookup",
        tool="stock_get_product",
        status="pending",
        args={"sku": "fl-da-dan"},
        depends_on=[1],
        parallel_group=7,
        hypotheses=["Follow the catalogue identifier with exact detail retrieval."],
    )

    assert step.depends_on == [1]
    assert step.parallel_group == 7


def test_agent_query_response_accepts_debug_payload() -> None:
    payload = AgentQueryResponse(
        status="answered",
        answer="Resolved the requested stock item.",
        debug=AgentDebugPayload(
            intent=AgentDebugIntent(
                current_goal="Resolve white gloss dance floor dimensions",
                primary_entity_guess="variant",
                requested_attributes=["size"],
                inferred_filters={"search": "white gloss dance floor"},
                scope_status="stock_supported",
            ),
            plan=AgentDebugPlan(
                goal="Resolve floor dimensions",
                status="complete",
                ready_steps=[],
                blocked_steps=[],
                dag=[
                    AgentDebugPlanStep(
                        id=1,
                        name="catalogue search",
                        tool="stock_search_catalogue",
                        status="done",
                        depends_on=[],
                        parallel_group=None,
                    )
                ],
                next_hop_rules=["Follow catalogue identifiers with exact lookups when detail is missing."],
            ),
            retrieval=AgentDebugRetrieval(
                thought_blocks=["<thought>\ngoal: resolve floor\nentity_guess: variant\nstrategy: catalogue search\ntool: stock_search_catalogue\nargs_draft: {'search': 'white gloss dance floor'}\nrisk: none\n</thought>"],
            ),
            grounding=AgentDebugGrounding(
                resolved_identifiers=["fl-da-dan"],
                evidence_count=1,
                unresolved_attributes=[],
                user_impact_limitations=[],
            ),
        ),
        session_state=SessionState(session_id="debug-contract"),
    )

    assert payload.debug is not None
    assert payload.debug.plan.dag[0].tool == "stock_search_catalogue"
    assert payload.debug.grounding.evidence_count == 1
