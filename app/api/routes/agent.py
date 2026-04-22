from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.dependencies import get_container
from app.schemas import AgentQueryRequest


router = APIRouter()
logger = logging.getLogger("hth")


@router.post("/query")
async def query_agent(payload: AgentQueryRequest, container = Depends(get_container)) -> dict[str, object]:
    logger.info(
        "ui_query_received session_id=%s message_preview=%s",
        payload.sessionId or "new",
        " ".join(payload.message.split())[:120],
    )
    result = await container.orchestrator_service.handle_query(payload)
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
    return result.model_dump(mode="json")
