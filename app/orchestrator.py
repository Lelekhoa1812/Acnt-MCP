from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from app.agent import AgentEngine
from app.config import Settings, UpstreamServiceError
from app.text.stopwords import STOPWORDS
from app.schemas import (
    AgentQueryRequest,
    AgentQueryResponse,
    PlanStatus,
    PlanStep,
    SessionState,
    ToolResult,
)
from app.session.store import SessionStore
from app.session.topic import apply_virtual_pruning, derive_memory_scope, refresh_active_subject
from app.tool.registry import ToolRegistry


@dataclass
class SessionNameCandidate:
    name: str | None
    raw_content: str
    finish_reason: str | None
    rejection_reason: str | None


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
        should_assign_session_name = self._should_assign_session_name(session_state)

        memory_scope = derive_memory_scope(request.message, session_state)
        apply_virtual_pruning(session_state, memory_scope)
        self.logger.info(
            "memory_scope_decision session_id=%s transition=%s target=%s allow_background=%s bridges=%s",
            session_state.session_id,
            memory_scope.transition,
            memory_scope.target_entity or "<none>",
            memory_scope.allow_background_reference,
            ",".join(memory_scope.bridge_signals) or "<none>",
        )

        run = await self.agent_engine.run(request=request, session_state=session_state)
        refresh_active_subject(
            session_state,
            request_message=request.message,
            target_entity=memory_scope.target_entity,
        )

        # Motivation vs Logic: naming now runs after the agent completes so we can
        # include resolved evidence/candidates in the LLM prompt instead of relying
        # on the initial prompt alone, which keeps us from falling back to the first
        # four words for every session.
        if should_assign_session_name:
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
            mock_ui_path = f"{self.settings.api_prefix}/chat"

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

    def _should_assign_session_name(self, session_state: SessionState) -> bool:
        # Root Cause vs Logic: session naming used to rerun on later turns when the
        # first-title source was `fallback`, which let follow-up prompts overwrite a
        # session that was already established. We now gate naming to untouched
        # sessions only, using pre-query state before this request mutates memory.
        if session_state.session_name or session_state.name_assigned or session_state.session_name_source:
            return False
        if session_state.conversation_history:
            return False
        if session_state.active_subject or session_state.background_subjects:
            return False
        if session_state.recent_product_names or session_state.recent_resolved_identifiers:
            return False
        if session_state.last_candidate_list or session_state.last_filters:
            return False
        if session_state.plan_todo or session_state.current_plan is not None:
            return False
        if session_state.memo_cache.entries or session_state.memo_cache.aggregates:
            return False
        if (
            session_state.plan_metadata.sorted_priorities
            or session_state.plan_metadata.confidence_scores
            or session_state.plan_metadata.validation_findings
        ):
            return False
        return True

    async def _ensure_session_name(self, session_state: SessionState, message: str) -> None:
        compact = message.strip()
        if not compact:
            return

        fallback_name = self._fallback_name(session_state, compact)
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
            candidate = await self._build_session_name(compact, session_state, naming_model)
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
        if candidate.name:
            session_state.session_name = candidate.name
            session_state.name_assigned = True
            session_state.session_name_source = "llm"
            self.logger.info(
                "Session naming assigned via llm session_id=%s title=%s finish_reason=%s",
                session_state.session_id,
                candidate.name,
                candidate.finish_reason or "unknown",
            )
            return

        if fallback_name:
            session_state.session_name = fallback_name
            session_state.name_assigned = True
            session_state.session_name_source = "fallback"
            raw_preview = self._truncate_words(candidate.raw_content, 8) if candidate.raw_content else "<empty>"
            self.logger.warning(
                "Session naming fallback applied (%s) session_id=%s title=%s raw_title=%s finish_reason=%s",
                candidate.rejection_reason or "empty llm output",
                session_state.session_id,
                fallback_name,
                raw_preview,
                candidate.finish_reason or "unknown",
            )

    async def _build_session_name(
        self,
        message: str,
        session_state: SessionState,
        model: str,
    ) -> SessionNameCandidate:
        snippet = self._truncate_words(message, 50)
        if not snippet:
            return SessionNameCandidate(
                name=None,
                raw_content="",
                finish_reason=None,
                rejection_reason="empty request snippet",
            )

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
                    "Return plain text only and never return an empty response. "
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

        response = await self._complete_session_name(model=model, messages=payload)
        choice = (response.get("choices") or [{}])[0]
        raw_content = self._coerce_completion_content((choice.get("message") or {}).get("content", ""))
        normalized = self._normalize_name(raw_content)
        finish_reason = choice.get("finish_reason")
        if not normalized and finish_reason == "length":
            response = await self._complete_session_name(model=model, messages=payload, max_completion_tokens=80)
            choice = (response.get("choices") or [{}])[0]
            raw_content = self._coerce_completion_content((choice.get("message") or {}).get("content", ""))
            normalized = self._normalize_name(raw_content)
            finish_reason = choice.get("finish_reason")
        return SessionNameCandidate(
            name=normalized,
            raw_content=raw_content,
            finish_reason=finish_reason,
            rejection_reason=self._classify_name_rejection(raw_content, normalized, finish_reason),
        )

    async def _complete_session_name(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_completion_tokens: int = 40,
    ) -> dict[str, Any]:
        return await self.agent_engine.complete_with_model(
            model=model,
            messages=messages,
            max_completion_tokens=max_completion_tokens,
        )

    def _coerce_completion_content(self, content: Any) -> str:
        # Root Cause vs Logic: Azure chat completions can return message content as
        # plain text or structured parts. Session naming treated anything that was
        # not a non-empty string as "empty", which hid whether the model emitted a
        # short title, structured text parts, or nothing at all.
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                nested = item.get("text", {})
                if isinstance(nested, dict) and isinstance(nested.get("value"), str):
                    parts.append(nested["value"])
            return " ".join(part.strip() for part in parts if part and part.strip()).strip()
        return ""

    def _classify_name_rejection(self, raw: str, normalized: str | None, finish_reason: str | None = None) -> str | None:
        if normalized:
            return None
        if finish_reason == "length":
            return "truncated llm output"
        if not raw.strip():
            return "empty llm output"
        if len([token for token in raw.split() if token]) < 2:
            return "too short llm output"
        return "unusable llm output"

    def _truncate_words(self, text: str, limit: int) -> str:
        tokens = [token for token in text.split() if token]
        return " ".join(tokens[:limit])

    def _normalize_name(self, raw: str) -> str | None:
        tokens = [token for token in raw.split() if token]
        if len(tokens) < 2:
            return None
        selection = tokens[:4]
        return " ".join(selection)

    def _fallback_name(self, session_state: SessionState, message: str) -> str | None:
        # Root Cause vs Logic: the previous fallback always copied the first
        # four words of the request, which produced unusable titles for common
        # lead-ins like "let me know..." even when we already had better product
        # context. We now prefer grounded session evidence, then strip known
        # conversational prefixes before falling back to the request opener.
        sources = [
            *session_state.recent_product_names,
            *(option.label for option in session_state.last_candidate_list if option.label),
            message,
        ]
        for source in sources:
            candidate = self._extract_title_from_source(source)
            if candidate:
                return candidate
        return None

    def _extract_title_from_source(self, source: str) -> str | None:
        source = source.strip()
        if not source:
            return None
        cleaned = self._strip_session_name_prefix(source)
        if cleaned != source:
            tokens = [
                token
                for token in cleaned.split()
                if token and token.lower() not in STOPWORDS and token.lower() not in self._session_name_fillers
            ]
            if tokens:
                return " ".join(tokens[:4])

        tokens = [token for token in source.split() if token]
        if not tokens:
            return None
        return " ".join(tokens[:4])

    def _strip_session_name_prefix(self, message: str) -> str:
        lowered = message.lower().strip()
        prefixes = (
            "please let me know ",
            "let me know ",
            "can you ",
            "could you ",
            "would you ",
            "tell me ",
            "show me ",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return message[len(prefix) :].strip()
        return message

    @property
    def _session_name_fillers(self) -> set[str]:
        return {
            "all",
            "about",
            "detail",
            "details",
            "let",
            "know",
            "me",
            "please",
            "show",
            "tell",
            "you",
        }
