from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UsageEvent(BaseModel):
    recorded_at: float
    kind: Literal["query", "tool"]
    tenant_id: str | None = None
    user_oid: str | None = None
    identity_key: str | None = None
    user_email: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    query: str | None = None
    tool_names: list[str] = Field(default_factory=list)


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
    events: list[UsageEvent] = Field(default_factory=list)


class UsageStatsSnapshot(BaseModel):
    generated_at: float
    groups: list[UsageUserGroup] = Field(default_factory=list)
