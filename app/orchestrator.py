from __future__ import annotations

import logging

from app.agent import AgentEngine
from app.config import Settings
from app.schemas import AgentQueryRequest, AgentQueryResponse, SessionState
from app.session.store import SessionStore
from app.tool.registry import ToolRegistry
from app.errors import UpstreamServiceError


class OrchestratorService:
    # Motivation vs Logic: the orchestrator is now a thin runtime coordinator
    # that delegates query planning to the model engine, persists session state,
    # and optionally returns the interactive mock UI entry point to callers.
    def __init__(
        self,
        settings: Settings,
        agent_engine: AgentEngine,
        tool_registry: ToolRegistry,
        session_store: SessionStore,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.agent_engine = agent_engine
        self.tool_registry = tool_registry
        self.session_store = session_store
        self.logger = logger

    async def handle_query(self, request: AgentQueryRequest) -> AgentQueryResponse:
        session_state, _ = await self.session_store.get_state(request.sessionId)
        if request.preferences:
            session_state.preferences.update(request.preferences)

        # Motivation vs Logic: new sessions get a short descriptive label from the
        # SLM before the agent starts, so dashboards can track intent without
        # waiting for the full conversational history.
        await self._ensure_session_name(session_state, request.message)

        run = await self.agent_engine.run(request=request, session_state=session_state)
        await self.session_store.save_state(session_state)

        mock_ui = None
        mock_ui_path = None
        if request.renderMockUi and self.settings.enable_mock_ui_simulation:
            mock_ui_path = f"{self.settings.api_prefix}/ui"

        return AgentQueryResponse(
            status=run.status,
            answer=run.answer,
            thoughts=run.thoughts if request.includeThoughts else [],
            tool_trace=run.tool_trace,
            clarification=run.clarification,
            resolved_items=run.resolved_items,
            session_state=session_state,
            mock_ui=mock_ui,
            mock_ui_path=mock_ui_path,
            limitations=run.limitations,
        )

    async def _ensure_session_name(self, session_state: SessionState, message: str) -> None:
        if session_state.session_name or session_state.name_assigned:
            return
        if not message.strip():
            return
        if not (self.settings.has_foundry and self.settings.has_slm_model):
            return
        try:
            name = await self._build_session_name(message, session_state)
        except UpstreamServiceError as exc:
            self.logger.warning("Session naming model failed: %s", exc)
            return
        except Exception as exc:  # pragma: no cover - defensive logging only
            self.logger.warning("Unexpected error while naming session: %s", exc)
            return

        # Root Cause vs Logic: naming used to mark the session as assigned before
        # the SLM call succeeded, so one transient timeout permanently disabled all
        # later retries for that session. We only lock the name once we actually
        # have a normalized value to persist back to the client and session store.
        if name:
            session_state.session_name = name
            session_state.name_assigned = True

    async def _build_session_name(self, message: str, session_state: SessionState) -> str | None:
        snippet = self._truncate_words(message, 50)
        if not snippet:
            return None

        # Motivation vs Logic: to keep the naming intelligence strictly inside
        # the LLM we only surface conversation artifacts as context text and let
        # the assistant decide which details matter for the session title.
        context_parts: list[str] = []
        if session_state.recent_product_names:
            context_parts.append(
                "Recent products: "
                + ", ".join(session_state.recent_product_names[:3])
            )
        if session_state.last_candidate_list:
            labels = [option.label for option in session_state.last_candidate_list[:3] if option.label]
            if labels:
                context_parts.append("Top candidates: " + ", ".join(labels))

        context_note = f"\n\nContext: {' | '.join(context_parts)}" if context_parts else ""

        payload = [
            {
                "role": "system",
                "content": (
                    "You are a session-naming assistant. Provide a concise, "
                    "business-focused title (2-4 words) that captures the user "
                    "goal without echoing the verbatim start of the request. "
                    "Use the context when available to mention the primary object "
                    "being requested (e.g., stock, variant, quote) and the action "
                    "the user expects the assistant to take."
                ),
            },
            {
                "role": "user",
                "content": f"Conversation snippet: {snippet}{context_note}",
            },
        ]

        response = await self.agent_engine.complete_with_model(
            model=self.settings.foundry_slm_model or "",
            messages=payload,
            max_completion_tokens=40,
        )
        content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        return self._normalize_name(content, snippet)

    def _truncate_words(self, text: str, limit: int) -> str:
        tokens = [token for token in text.split() if token]
        return " ".join(tokens[:limit])

    def _normalize_name(self, raw: str, fallback: str) -> str | None:
        tokens = [token for token in raw.split() if token]
        fallback_tokens = [token for token in fallback.split() if token]
        combined = tokens or fallback_tokens
        if not combined:
            return None
        selection = combined[:4]
        if len(selection) < 2 and fallback_tokens:
            selection = (selection + fallback_tokens)[:2]
        return " ".join(selection)
