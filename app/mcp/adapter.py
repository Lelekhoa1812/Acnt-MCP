from __future__ import annotations

import json
import logging
from typing import Any

from mcp import types
from mcp.shared.context import RequestContext

from app.config import (
    InventoryNotFoundError,
    ParameterMappingError,
    UnsupportedToolError,
    UpstreamServiceError,
)
from app.orchestrator import OrchestratorService
from app.schemas import ToolResult


class McpToolAdapter:
    # Motivation vs Logic: this adapter keeps the business-facing tool registry as
    # the single source of truth, while translating its schemas, session handling,
    # and results into protocol-native MCP `tools/list` and `tools/call` payloads.
    def __init__(self, orchestrator_service: OrchestratorService, default_session_id: str, logger: logging.Logger) -> None:
        self.orchestrator_service = orchestrator_service
        self.default_session_id = default_session_id
        self.logger = logger

    def list_tools(self) -> list[types.Tool]:
        return [
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
            )
            for tool in self.orchestrator_service.tool_registry.list_tools(include_hidden=False)
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        request_context: RequestContext[Any, Any, Any] | None = None,
    ) -> types.CallToolResult:
        session_id = self._resolve_session_id(request_context)
        payload = arguments or {}
        tool_name = self.orchestrator_service.tool_registry.resolve_tool_name(name)

        try:
            result = await self.orchestrator_service.call_tool_with_orchestration(
                tool_name=tool_name,
                args=payload,
                session_id=session_id,
            )
            self._log_success(result=result, session_id=session_id)
            return self._success_result(result)
        except (UnsupportedToolError, ParameterMappingError, InventoryNotFoundError, UpstreamServiceError) as exc:
            self._log_error(name=name, arguments=payload, session_id=session_id, exc=exc)
            return self._error_result(exc)
        except Exception as exc:  # pragma: no cover - defensive fallback
            self.logger.exception("mcp_tool_unhandled_error tool=%s session_id=%s", name, session_id)
            return self._error_result(RuntimeError(f"Unhandled MCP tool failure: {exc}"))

    def _resolve_session_id(self, request_context: RequestContext[Any, Any, Any] | None) -> str:
        if request_context is None:
            return self.default_session_id

        if request_context.meta and getattr(request_context.meta, "client_id", None):
            return f"mcp-client:{request_context.meta.client_id}"

        client_params = getattr(request_context.session, "client_params", None)
        client_info = getattr(client_params, "clientInfo", None)
        if client_info is not None and getattr(client_info, "name", None):
            return f"mcp-session:{client_info.name}:{id(request_context.session):x}"

        return f"mcp-session:{id(request_context.session):x}"

    def _success_result(self, result: ToolResult) -> types.CallToolResult:
        envelope: dict[str, Any] = {"data": result.data}
        if result.llm_content is not None:
            envelope["answer_ready"] = result.llm_content
        if result.normalization_notes:
            envelope["normalization_notes"] = result.normalization_notes
        if result.plan_status is not None:
            envelope["plan_status"] = result.plan_status.model_dump(mode="json")
        if result.memo_update is not None:
            envelope["memo_update"] = result.memo_update.model_dump(mode="json")
        if result.validation is not None:
            envelope["validation"] = result.validation.model_dump(mode="json")

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False, sort_keys=True))],
            structuredContent=envelope,
        )

    def _error_result(self, exc: Exception) -> types.CallToolResult:
        error: dict[str, Any] = {
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        if isinstance(exc, UpstreamServiceError):
            error["status_code"] = exc.status_code

        envelope = {"error": error}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False, sort_keys=True))],
            structuredContent=envelope,
            isError=True,
        )

    def _log_success(self, result: ToolResult, session_id: str) -> None:
        trace = result.trace
        if trace is None:
            self.logger.debug("mcp_tool_result tool=%s session_id=%s status=%s", result.tool, session_id, result.status)
            return

        self.logger.debug(
            "mcp_tool_result tool=%s session_id=%s status=%s cache_status=%s result_count=%s notes=%s",
            result.tool,
            session_id,
            result.status,
            trace.cache_status,
            trace.result_count,
            result.normalization_notes,
        )

    def _log_error(self, name: str, arguments: dict[str, Any], session_id: str, exc: Exception) -> None:
        self.logger.warning(
            "mcp_tool_error tool=%s session_id=%s args=%s error=%s",
            name,
            session_id,
            arguments,
            exc,
        )
