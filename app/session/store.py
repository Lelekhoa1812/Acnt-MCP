from __future__ import annotations

import logging

from app.config import Settings
from app.schemas import SessionState
from app.store import AppKeyValueStore


class SessionStore:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger

    async def get_state(self, session_id: str | None = None) -> tuple[SessionState, str]:
        resolved_session_id = session_id or self.settings.default_session_id
        cached, cache_status = await self.key_value_store.get_json("session", resolved_session_id)
        if cached is None:
            return SessionState(session_id=resolved_session_id), cache_status
        return SessionState.model_validate(cached), cache_status

    async def save_state(self, state: SessionState) -> str:
        return await self.key_value_store.set_json(
            namespace="session",
            key=state.session_id,
            value=state.model_dump(mode="json"),
            ttl_seconds=self.settings.session_ttl_seconds,
        )

    async def clear_state(self, session_id: str | None = None) -> tuple[SessionState, str]:
        resolved_session_id = session_id or self.settings.default_session_id
        cache_status = await self.key_value_store.delete("session", resolved_session_id)
        self.logger.debug("Cleared session state for session_id=%s", resolved_session_id)
        return SessionState(session_id=resolved_session_id), cache_status
