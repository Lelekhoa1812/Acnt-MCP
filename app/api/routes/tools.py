from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import get_container
from app.schemas import CallToolRequest


def build_tools_router() -> APIRouter:
    router = APIRouter()

    @router.get("/tools")
    async def list_tools(container = Depends(get_container)) -> dict[str, object]:
        return {"tools": [tool.model_dump(mode="json") for tool in container.tool_registry.list_tools()]}

    @router.post("/tools/call")
    async def call_tool(payload: CallToolRequest, container = Depends(get_container)) -> dict[str, object]:
        result = await container.tool_registry.call_tool(
            payload.tool,
            payload.args,
            session_id=payload.sessionId,
        )
        return result.model_dump(mode="json")

    return router
