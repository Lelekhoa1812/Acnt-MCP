from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.api.auth import resolve_user_context_or_none
from app.agent.engine import AgentRun
from app.config import (
    InventoryNotFoundError,
    ParameterMappingError,
    UnsupportedToolError,
    UpstreamServiceError,
    get_container,
)
from app.schemas import AgentQueryRequest


router = APIRouter()
logger = logging.getLogger("hth")


@router.post("/query")
async def query_agent(payload: AgentQueryRequest, request: Request, container = Depends(get_container)) -> dict[str, object]:
    user_context = resolve_user_context_or_none(request, container.settings)
    logger.info(
        "ui_query_received session_id=%s message_preview=%s",
        payload.sessionId or "new",
        " ".join(payload.message.split())[:120],
    )
    try:
        result = await container.orchestrator_service.handle_query(payload, user_context=user_context)
    except (InventoryNotFoundError, ParameterMappingError, UnsupportedToolError, UpstreamServiceError) as exc:
        logger.warning("ui_query_failure session_id=%s error=%s", payload.sessionId or "new", exc)
        fallback = AgentRun(
            status="error",
            answer="The request failed due to a backend issue.",
            limitations=[str(exc)],
            thoughts=[],
            tool_trace=[],
            resolved_items=[],
            plan_status=None,
        )
        return fallback.model_dump(mode="json")
    # Root Cause vs Logic: frontend-only logs made title debugging opaque from
    # server terminals; mirror the resolved title source in API logs so sidebar
    # naming issues can be traced request-by-request.
    fallback_guess = " ".join(payload.message.split()[:4])
    resolved_title = result.session_state.session_name or ""
    source_guess = "fallback_like" if resolved_title == fallback_guess else "llm_or_persisted"
    logger.info(
        "ui_query_title_resolved session_id=%s name_assigned=%s title=%s source_guess=%s fallback_guess=%s",
        result.session_state.session_id,
        result.session_state.name_assigned,
        resolved_title,
        source_guess,
        fallback_guess,
    )
    answer_preview = " ".join(result.answer.split())[:200]
    plan_status_label = result.plan_status.status if result.plan_status else "unknown"
    plan_steps = len(result.plan_status.steps) if result.plan_status else 0
    limitations_summary = ";".join(result.limitations) if result.limitations else "none"
    # Motivation vs Logic: summarise each response so we can track which status came back and why without recording the full payload.
    logger.info(
        "ui_query_response session_id=%s status=%s plan_status=%s plan_steps=%s clarification=%s answer_preview=%s limitations=%s",
        result.session_state.session_id,
        result.status,
        plan_status_label,
        plan_steps,
        bool(result.clarification),
        answer_preview,
        limitations_summary,
    )
    return result.model_dump(mode="json")
