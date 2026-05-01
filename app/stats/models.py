from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UsageEvent(BaseModel):
    recorded_at: float
    kind: Literal["query", "tool", "tool_error"]
    tenant_id: str | None = None
    user_oid: str | None = None
    identity_key: str | None = None
    user_email: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    group_names: list[str] = Field(default_factory=list)
    query: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    error_status_code: int | None = None
    error_message: str | None = None
    error_request: str | None = None


class UsageClientSummary(BaseModel):
    label: str
    ai_key: str
    count: int = 0


class UsageToolClientSummary(BaseModel):
    label: str
    ai_key: str
    count: int = 0


class UsageToolSummary(BaseModel):
    name: str
    count: int = 0
    clients: list[UsageToolClientSummary] = Field(default_factory=list)


class UsageToolErrorSummary(BaseModel):
    recorded_at: float
    identity_label: str
    user_email: str | None = None
    client_label: str
    ai_key: str
    tool_name: str
    query: str | None = None
    error_request: str | None = None
    error_status_code: int | None = None
    error_message: str | None = None


class UsageUserGroup(BaseModel):
    identity_label: str
    tenant_id: str | None = None
    user_oid: str | None = None
    identity_key: str | None = None
    user_email: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    group_names: list[str] = Field(default_factory=list)
    matched_groups: list[str] = Field(default_factory=list)
    clients: list[UsageClientSummary] = Field(default_factory=list)
    tools: list[UsageToolSummary] = Field(default_factory=list)
    events: list[UsageEvent] = Field(default_factory=list)


class UsageStatsSnapshot(BaseModel):
    generated_at: float
    groups: list[UsageUserGroup] = Field(default_factory=list)
    tool_errors: list[UsageToolErrorSummary] = Field(default_factory=list)


class ToolDurationRecord(BaseModel):
    recorded_at: float
    tool: str
    duration_seconds: float
    client_label: str | None = None
    ai_key: str = Field(default="other")
