from __future__ import annotations

import logging
from typing import Any

from app.agent import AgentEngine
from app.config import Settings, UpstreamServiceError
from app.schemas import (
    AgentQueryRequest,
    AgentQueryResponse,
    PlanStatus,
    PlanStep,
    SessionState,
    ToolResult,
)
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

        # Motivation vs Logic: naming now runs after the agent completes so we can
        # include resolved evidence/candidates in the LLM prompt instead of relying
        # on the initial prompt alone, which keeps us from falling back to the first
        # four words for every session.
        await self._ensure_session_name(session_state, request.message)
        await self.session_store.save_state(session_state)
        if self.settings.local_chat_memory_enabled:
            await self.session_store.save_local_chat_turn(
                session_id=session_state.session_id,
                user_message=request.message,
                assistant_message=run.answer,
            )

        mock_ui = None
        mock_ui_path = None
        if request.renderMockUi and self.settings.enable_mock_ui_simulation:
            mock_ui_path = f"{self.settings.api_prefix}/ui"

        return AgentQueryResponse(
            status=run.status,
            answer=run.answer,
            thoughts=run.thoughts if request.includeThoughts else [],
            debug=run.debug if request.includeThoughts else None,
            tool_trace=run.tool_trace,
            clarification=run.clarification,
            resolved_items=run.resolved_items,
            session_state=session_state,
            plan_status=run.plan_status,
            mock_ui=mock_ui,
            mock_ui_path=mock_ui_path,
            limitations=run.limitations,
        )

    async def call_tool_with_orchestration(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        session_id: str | None,
        thought: str = "",
    ) -> ToolResult:
        # Motivation vs Logic: direct REST/MCP tool calls now flow through the
        # same session-scoped plan+memo+validation lifecycle as `/query`, so the
        # runtime can resume TODO/memo evidence across mixed invocation styles.
        session_state, _ = await self.session_store.get_state(session_id)
        plan_status = self._resolve_direct_tool_plan(session_state, tool_name)
        step = self._append_direct_tool_step(plan_status, tool_name, args)
        step.status = "in-progress"
        self.agent_engine.persist_plan_state(session_state, plan_status)

        try:
            result = await self.tool_registry.call_tool(
                tool_name,
                args,
                session_id=session_state.session_id,
                thought=thought,
            )
        except Exception:
            await self.session_store.save_state(session_state)
            raise

        validation, memo_update, validation_limitations = await self.agent_engine.validate_and_record(
            session_state=session_state,
            plan_status=plan_status,
            step=step,
            tool_name=tool_name,
            tool_args=args,
            result=result,
            use_model_validator=False,
        )
        step.validation = validation
        step.status = "done"
        plan_status.status = "complete" if all(item.status == "done" for item in plan_status.steps) else "in-progress"
        self.agent_engine.persist_plan_state(session_state, plan_status)

        if validation_limitations:
            result.normalization_notes = self._dedupe(result.normalization_notes + validation_limitations)
        result.plan_status = plan_status
        result.memo_update = memo_update
        result.validation = validation

        await self.session_store.save_state(session_state)
        return result

    def _resolve_direct_tool_plan(self, session_state: SessionState, tool_name: str) -> PlanStatus:
        if session_state.current_plan is not None:
            return session_state.current_plan
        return PlanStatus(
            goal=f"Direct tool orchestration for `{tool_name}`",
            steps=[],
            memo=session_state.memo_cache.model_copy(deep=True),
            status="in-progress",
        )

    def _append_direct_tool_step(
        self,
        plan_status: PlanStatus,
        tool_name: str,
        args: dict[str, Any],
    ) -> PlanStep:
        next_id = max((step.id for step in plan_status.steps), default=0) + 1
        step = PlanStep(
            id=next_id,
            name=f"direct tool call {next_id}",
            tool=tool_name,
            status="pending",
            args=args,
            hypotheses=["Direct invocation requested by client endpoint."],
            validation=None,
        )
        plan_status.steps.append(step)
        return step

    def _dedupe(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    async def _ensure_session_name(self, session_state: SessionState, message: str) -> None:
        compact = message.strip()
        if not compact:
            return

        fallback_name = self._fallback_name(compact)
        if session_state.session_name and session_state.session_name_source != "fallback":
            return

        naming_model = self.settings.foundry_slm_model or self.settings.foundry_model
        if not (self.settings.has_foundry and naming_model):
            if fallback_name:
                session_state.session_name = fallback_name
                session_state.name_assigned = True
                session_state.session_name_source = "fallback"
                self.logger.warning(
                    "Session naming fallback applied (config unavailable) session_id=%s title=%s",
                    session_state.session_id,
                    fallback_name,
                )
            return
        try:
            name = await self._build_session_name(compact, session_state, naming_model)
        except UpstreamServiceError as exc:
            if fallback_name:
                session_state.session_name = fallback_name
                session_state.name_assigned = True
                session_state.session_name_source = "fallback"
                self.logger.warning(
                    "Session naming fallback applied (upstream) session_id=%s title=%s reason=%s",
                    session_state.session_id,
                    fallback_name,
                    exc,
                )
            else:
                self.logger.warning("Session naming model failed: %s", exc)
            return
        except Exception as exc:  # pragma: no cover - defensive logging only
            if fallback_name:
                session_state.session_name = fallback_name
                session_state.name_assigned = True
                session_state.session_name_source = "fallback"
                self.logger.warning(
                    "Session naming fallback applied (unexpected) session_id=%s title=%s reason=%s",
                    session_state.session_id,
                    fallback_name,
                    exc,
                )
            else:
                self.logger.warning("Unexpected error while naming session: %s", exc)
            return

        # Root Cause vs Logic: session naming previously appeared to "work" by
        # silently using a local first-words shortcut even when LLM naming failed.
        # We now treat that shortcut as explicit fallback and only apply it on
        # real naming errors; successful runs must persist the LLM output.
        if name:
            session_state.session_name = name
            session_state.name_assigned = True
            session_state.session_name_source = "llm"
            self.logger.info(
                "Session naming assigned via llm session_id=%s title=%s",
                session_state.session_id,
                name,
            )
            return

        if fallback_name:
            session_state.session_name = fallback_name
            session_state.name_assigned = True
            session_state.session_name_source = "fallback"
            self.logger.warning(
                "Session naming fallback applied (empty llm output) session_id=%s title=%s",
                session_state.session_id,
                fallback_name,
            )

    async def _build_session_name(self, message: str, session_state: SessionState, model: str) -> str | None:
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
            model=model,
            messages=payload,
            max_completion_tokens=40,
        )
        content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        return self._normalize_name(content)

    def _truncate_words(self, text: str, limit: int) -> str:
        tokens = [token for token in text.split() if token]
        return " ".join(tokens[:limit])

    def _normalize_name(self, raw: str) -> str | None:
        tokens = [token for token in raw.split() if token]
        if len(tokens) < 2:
            return None
        selection = tokens[:4]
        return " ".join(selection)

    def _fallback_name(self, message: str) -> str | None:
        tokens = [token for token in message.split() if token]
        if not tokens:
            return None
        return " ".join(tokens[:4])
