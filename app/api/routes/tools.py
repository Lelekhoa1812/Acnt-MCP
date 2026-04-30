from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import resolve_user_context_or_none
from app.auth import IdentityAuthError
from app.config import get_container
from app.config.settings import Settings
from app.schemas import CallToolRequest


def build_tools_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/tools")
    async def list_tools(request: Request, container=Depends(get_container)) -> dict[str, object]:
        user_context = resolve_user_context_or_none(request, settings)
        return {
            "tools": [
                tool.model_dump(mode="json")
                for tool in container.tool_registry.list_tools(
                    include_hidden=False,
                    user_context=user_context,
                )
            ]
        }

    @router.post("/tools/call")
    async def call_tool(payload: CallToolRequest, request: Request, container=Depends(get_container)) -> dict[str, object]:
        user_context = resolve_user_context_or_none(request, settings)
        try:
            result = await container.orchestrator_service.call_tool_with_orchestration(
                tool_name=payload.tool,
                args=payload.args,
                session_id=payload.sessionId,
                user_context=user_context,
            )
        except IdentityAuthError as exc:
            raise HTTPException(status_code=exc.payload.status_code, detail=exc.to_response_payload()["error"]) from exc
        return result.model_dump(mode="json")

    return router
