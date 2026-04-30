from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UsageEvent(BaseModel):
    recorded_at: float
    kind: Literal["query", "tool"]
    user_oid: str | None = None
    user_email: str | None = None
    query: str | None = None
    tool_names: list[str] = Field(default_factory=list)


class UsageUserGroup(BaseModel):
    identity_label: str
    user_oid: str | None = None
    user_email: str | None = None
    events: list[UsageEvent] = Field(default_factory=list)


class UsageStatsSnapshot(BaseModel):
    generated_at: float
    groups: list[UsageUserGroup] = Field(default_factory=list)
