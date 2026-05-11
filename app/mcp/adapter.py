from __future__ import annotations

import json
import logging
from typing import Any

from mcp import types

from app.config import (
    ParameterMappingError,
    UnsupportedToolError,
    UpstreamServiceError,
)
from app.mcp.output import compact_success_envelope
from app.schemas import ToolResult
from app.tool.registry import ToolRegistry


class McpToolAdapter:
    def __init__(self, tool_registry: ToolRegistry, logger: logging.Logger) -> None:
        self.tool_registry = tool_registry
        self.logger = logger

    def list_tools(self) -> list[types.Tool]:
        tools: list[types.Tool] = []
        for tool in self.tool_registry.list_tools(include_hidden=False):
            kwargs: dict[str, Any] = {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            if tool.output_schema is not None:
                kwargs["outputSchema"] = tool.output_schema
            tools.append(types.Tool(**kwargs))
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
    ) -> types.CallToolResult:
        payload = arguments or {}
        try:
            tool_name = self.tool_registry.resolve_tool_name(name)
            result = await self.tool_registry.call_tool(tool_name, payload)
            self._log_success(result)
            return self._success_result(result)
        except (UnsupportedToolError, ParameterMappingError, UpstreamServiceError) as exc:
            self._log_error(name=name, arguments=payload, exc=exc)
            return self._error_result(exc)
        except Exception as exc:  # pragma: no cover - defensive fallback
            self.logger.exception("mcp_tool_unhandled_error tool=%s", name)
            return self._error_result(RuntimeError(f"Unhandled MCP tool failure: {exc}"))

    def _success_result(self, result: ToolResult) -> types.CallToolResult:
        envelope: dict[str, Any] = {"data": result.data}
        if result.llm_content is not None:
            envelope["answer_ready"] = result.llm_content
        if result.normalization_notes:
            envelope["normalization_notes"] = result.normalization_notes
        envelope = compact_success_envelope(envelope)

        content: list[
            types.TextContent
            | types.ImageContent
            | types.AudioContent
            | types.ResourceLink
            | types.EmbeddedResource
        ]
        if result.mcp_content:
            content = [
                types.ImageContent(type=item.type, data=item.data, mimeType=item.mimeType)
                for item in result.mcp_content
            ]
        else:
            content = [types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False, sort_keys=True))]
        return types.CallToolResult(
            content=content,
            structuredContent=envelope,
            isError=False,
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

    def _log_success(self, result: ToolResult) -> None:
        trace = result.trace
        if trace is None:
            self.logger.debug("mcp_tool_result tool=%s status=%s", result.tool, result.status)
            return
        self.logger.debug(
            "mcp_tool_result tool=%s status=%s cache_status=%s result_count=%s notes=%s",
            result.tool,
            result.status,
            trace.cache_status,
            trace.result_count,
            result.normalization_notes,
        )

    def _log_error(self, name: str, arguments: dict[str, Any], exc: Exception) -> None:
        self.logger.warning("mcp_tool_error tool=%s args=%s error=%s", name, arguments, exc)
