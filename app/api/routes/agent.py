from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_container
from app.schemas import AgentQueryRequest


router = APIRouter()


@router.post("/query")
async def query_agent(payload: AgentQueryRequest, container = Depends(get_container)) -> dict[str, object]:
    result = await container.orchestrator_service.handle_query(payload)
    return result.model_dump(mode="json")
