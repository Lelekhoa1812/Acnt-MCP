from __future__ import annotations

import json
import logging
from typing import Any

from mcp import types
from mcp.shared.context import RequestContext

from app.auth import IdentityAuthError, get_user_context
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
        user_context = get_user_context()
        return [
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
            )
            for tool in self.orchestrator_service.tool_registry.list_tools(
                include_hidden=False,
                user_context=user_context,
            )
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        request_context: RequestContext[Any, Any, Any] | None = None,
    ) -> types.CallToolResult:
        session_id = self._resolve_session_id(request_context)
        client_id, client_name = self._resolve_client_identity(request_context)
        payload = arguments or {}
        tool_name = self.orchestrator_service.tool_registry.resolve_tool_name(name)
        user_context = get_user_context()

        try:
            result = await self.orchestrator_service.call_tool_with_orchestration(
                tool_name=tool_name,
                args=payload,
                session_id=session_id,
                user_context=user_context,
                client_id=client_id,
                client_name=client_name,
            )
            self._log_success(
                result=result,
                client_id=client_id,
                client_name=client_name,
            )
            return self._success_result(result)
        except (UnsupportedToolError, ParameterMappingError, InventoryNotFoundError, UpstreamServiceError, IdentityAuthError) as exc:
            self._log_error(
                name=name,
                arguments=payload,
                client_id=client_id,
                client_name=client_name,
                exc=exc,
            )
            return self._error_result(exc)
        except Exception as exc:  # pragma: no cover - defensive fallback
            self.logger.exception("mcp_tool_unhandled_error tool=%s session_id=%s", name, session_id)
            return self._error_result(RuntimeError(f"Unhandled MCP tool failure: {exc}"))

    def _resolve_session_id(self, request_context: RequestContext[Any, Any, Any] | None) -> str:
        user_context = get_user_context()
        if user_context is not None:
            return user_context.session_key

        if request_context is None:
            return self.default_session_id

        meta = getattr(request_context, "meta", None)
        # Root Cause vs Logic: some stdio and test request contexts do not
        # populate `meta`, so we must guard this lookup before reading the
        # client identifier.
        if meta is not None and getattr(meta, "client_id", None):
            return f"mcp-client:{meta.client_id}"

        client_params = getattr(request_context.session, "client_params", None)
        client_info = getattr(client_params, "clientInfo", None)
        if client_info is not None and getattr(client_info, "name", None):
            return f"mcp-session:{client_info.name}:{id(request_context.session):x}"

        return f"mcp-session:{id(request_context.session):x}"

    def _resolve_client_identity(self, request_context: RequestContext[Any, Any, Any] | None) -> tuple[str | None, str | None]:
        # Motivation vs Logic: the same request can arrive from a remote OAuth
        # connector or a local stdio client, so we resolve the best available
        # connector labels once and reuse them in every log branch.
        if request_context is None:
            return None, None

        client_id = None
        client_name = None

        meta = getattr(request_context, "meta", None)
        if meta is not None:
            client_id = getattr(meta, "client_id", None)

        client_params = getattr(request_context.session, "client_params", None)
        client_info = getattr(client_params, "clientInfo", None)
        if client_info is not None:
            client_name = getattr(client_info, "name", None)

        return client_id, client_name

    def _resolve_user_identity(self) -> tuple[str | None, str | None]:
        user_context = get_user_context()
        if user_context is None:
            return None, None
        return user_context.oid, user_context.email

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

        # Motivation vs Logic: stock image previews should give MCP clients the
        # encoded image block before fallback instructions, while structuredContent
        # still carries the JSON contract for clients that read tool metadata first.
        content: list[
            types.TextContent
            | types.ImageContent
            | types.AudioContent
            | types.ResourceLink
            | types.EmbeddedResource
        ] = [
            types.ImageContent(type=item.type, data=item.data, mimeType=item.mimeType)
            for item in result.mcp_content
        ]
        content.append(types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False, sort_keys=True)))
        return types.CallToolResult(
            content=content,
            structuredContent=envelope,
        )

    def _error_result(self, exc: Exception) -> types.CallToolResult:
        error: dict[str, Any] = {
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        if isinstance(exc, UpstreamServiceError):
            error["status_code"] = exc.status_code
        if isinstance(exc, IdentityAuthError):
            payload = exc.to_response_payload()["error"]
            error.update(payload)

        envelope = {"error": error}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False, sort_keys=True))],
            structuredContent=envelope,
            isError=True,
        )

    def _log_success(
        self,
        result: ToolResult,
        client_id: str | None,
        client_name: str | None,
    ) -> None:
        user_oid, user_email = self._resolve_user_identity()
        trace = result.trace
        if trace is None:
            self.logger.debug(
                "mcp_tool_result tool=%s user_oid=%s user_email=%s client_id=%s client_name=%s status=%s",
                result.tool,
                user_oid,
                user_email,
                client_id,
                client_name,
                result.status,
            )
            return

        self.logger.debug(
            "mcp_tool_result tool=%s user_oid=%s user_email=%s client_id=%s client_name=%s status=%s cache_status=%s result_count=%s notes=%s",
            result.tool,
            user_oid,
            user_email,
            client_id,
            client_name,
            result.status,
            trace.cache_status,
            trace.result_count,
            result.normalization_notes,
        )

    def _log_error(
        self,
        name: str,
        arguments: dict[str, Any],
        client_id: str | None,
        client_name: str | None,
        exc: Exception,
    ) -> None:
        user_oid, user_email = self._resolve_user_identity()
        self.logger.warning(
            "mcp_tool_error tool=%s user_oid=%s user_email=%s client_id=%s client_name=%s args=%s error=%s",
            name,
            user_oid,
            user_email,
            client_id,
            client_name,
            arguments,
            exc,
        )
