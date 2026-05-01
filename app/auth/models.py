from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UserContext(BaseModel):
    tenant_id: str
    user_id: str
    subject: str
    # Motivation vs Logic: oid is the stable Entra object id we want to surface
    # beside email in logs, while user_id may still fall back to subject data in
    # other identity flows.
    oid: str | None = None
    # Motivation vs Logic: remote OAuth connectors sometimes expose an email-like
    # claim that is useful for log correlation, so we keep it on the shared
    # user context instead of re-parsing JWT claims in each call site.
    email: str | None = None
    client_id: str | None = None
    token_origin: str | None = None
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    group_names: list[str] = Field(default_factory=list)
    plugin_permissions: list[str] = Field(default_factory=list)
    # Department-based access is disabled for now, so the claim is kept
    # commented out rather than removed.
    # department_claim: str | None = None
    claims: dict[str, Any] = Field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"mcp:session:{self.tenant_id}:{self.user_id}"
