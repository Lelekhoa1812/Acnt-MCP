from __future__ import annotations

import logging

from app.agent import AgentEngine
from app.config import Settings
from app.schemas import AgentQueryRequest, AgentQueryResponse
from app.session.store import SessionStore
from app.tool.registry import ToolRegistry


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

        run = await self.agent_engine.run(request=request, session_state=session_state)
        await self.session_store.save_state(session_state)

        mock_ui = None
        mock_ui_path = None
        if request.renderMockUi and self.settings.enable_mock_ui_simulation:
            mock_ui_path = f"{self.settings.api_prefix}/mock-ui"

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
