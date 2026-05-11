from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolTrace(BaseModel):
    thought: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str
    error_status_code: int | None = None
    error_request: str | None = None
    cache_status: str | None = None
    source_data: str | None = None
    result_count: int | None = None
    normalization_notes: list[str] = Field(default_factory=list)
    duration_seconds: float | None = None


class McpImageContent(BaseModel):
    type: Literal["image"] = "image"
    data: str
    mimeType: str


class ToolResult(BaseModel):
    tool: str
    status: str = "ok"
    data: Any
    llm_content: Any | None = Field(default=None, exclude=True)
    mcp_content: list[McpImageContent] = Field(default_factory=list, exclude=True)
    normalization_notes: list[str] = Field(default_factory=list)
    trace: ToolTrace | None = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class CallToolRequest(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
