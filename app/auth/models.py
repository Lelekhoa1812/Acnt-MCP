from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UserContext(BaseModel):
    tenant_id: str
    user_id: str
    subject: str
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    # Department-based access is disabled for now, so the claim is kept
    # commented out rather than removed.
    # department_claim: str | None = None
    claims: dict[str, Any] = Field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"mcp:session:{self.tenant_id}:{self.user_id}"
