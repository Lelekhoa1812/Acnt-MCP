from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
from app.schemas import ConversationTurn, SessionState
from app.store import AppKeyValueStore, InMemoryTtlStore


class SessionStore:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger
        self._local_chat_history = InMemoryTtlStore()

    async def get_state(self, session_id: str | None = None) -> tuple[SessionState, str]:
        resolved_session_id = session_id or self.settings.default_session_id
        cached, cache_status = await self.key_value_store.get_json("session", resolved_session_id)
        if cached is None:
            state = SessionState(session_id=resolved_session_id)
        else:
            state = SessionState.model_validate(cached)

        if self.settings.local_chat_memory_enabled:
            state.conversation_history = await self._get_local_chat_history(resolved_session_id)
        return state, cache_status

    async def save_state(self, state: SessionState) -> str:
        return await self.key_value_store.set_json(
            namespace="session",
            key=state.session_id,
            value=state.model_dump(mode="json"),
            ttl_seconds=self.settings.session_ttl_seconds,
        )

    async def save_local_chat_turn(self, session_id: str, user_message: str, assistant_message: str) -> str:
        if not self.settings.local_chat_memory_enabled:
            return "local_memory_disabled"

        # Motivation vs Logic: local REST development lacks Claude-managed chat
        # history, so we persist only recent user/assistant turns in process
        # memory (device-local) and keep Redis reserved for shared app caches.
        history = await self._get_local_chat_history(session_id)
        user_compact = user_message.strip()
        assistant_compact = assistant_message.strip()
        if user_compact:
            history.append(ConversationTurn(role="user", content=user_compact))
        if assistant_compact:
            history.append(ConversationTurn(role="assistant", content=assistant_compact))

        max_messages = self.settings.local_chat_memory_turns * 2
        trimmed = history[-max_messages:]
        await self._set_local_chat_history(session_id, trimmed)
        return "local_memory_set"

    async def clear_state(self, session_id: str | None = None) -> tuple[SessionState, str]:
        resolved_session_id = session_id or self.settings.default_session_id
        cache_status = await self.key_value_store.delete("session", resolved_session_id)
        await self._local_chat_history.delete(self._chat_history_key(resolved_session_id))
        self.logger.debug("Cleared session state for session_id=%s", resolved_session_id)
        return SessionState(session_id=resolved_session_id), cache_status

    async def _get_local_chat_history(self, session_id: str) -> list[ConversationTurn]:
        raw = await self._local_chat_history.get(self._chat_history_key(session_id))
        if raw is None:
            return []
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            self.logger.warning("Local chat history decode failed for session_id=%s", session_id)
            return []
        if not isinstance(parsed, list):
            return []

        turns: list[ConversationTurn] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                turns.append(ConversationTurn.model_validate(item))
            except Exception:  # pragma: no cover - defensive data guard
                continue
        return turns

    async def _set_local_chat_history(self, session_id: str, turns: list[ConversationTurn]) -> None:
        serialized = json.dumps([turn.model_dump(mode="json") for turn in turns], sort_keys=True, ensure_ascii=False)
        await self._local_chat_history.set(
            self._chat_history_key(session_id),
            serialized,
            self.settings.session_ttl_seconds,
        )

    def _chat_history_key(self, session_id: str) -> str:
        return f"session_chat_history:{session_id}"
