from __future__ import annotations

from typing import Any


def resolve_user_email(claims: dict[str, Any]) -> str | None:
    # Motivation vs Logic: different IdPs and connector flows surface the
    # user mailbox under different claim names, so we centralize the fallback
    # order once and reuse it across auth and bridge token issuance.
    for claim_name in ("email", "preferred_username", "upn", "unique_name"):
        raw_value = claims.get(claim_name)
        if raw_value is None:
            continue
        rendered_value = raw_value.strip() if isinstance(raw_value, str) else str(raw_value).strip()
        if rendered_value:
            return rendered_value
    return None
