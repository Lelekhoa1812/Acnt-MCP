from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import anyio
import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import InventoryNotFoundError, ParameterMappingError, Settings, UpstreamServiceError
from app.tool.stock.presenter import render_inventory_snapshot_markdown
from app.prompt import render_composer, render_formatter, render_planner, render_system, render_validator
from app.schemas import (
    AgentDebugGrounding,
    AgentDebugIntent,
    AgentDebugParallelBatch,
    AgentDebugPayload,
    AgentDebugPlan,
    AgentDebugPlanStep,
    AgentDebugRetrieval,
    AgentDebugTraceSummary,
    AgentQueryRequest,
    CandidateOption,
    ClarificationPayload,
    MemoEntry,
    NormalizedEvidence,
    PlanStatus,
    PlanStep,
    PlanValidation,
    SessionState,
    ThoughtBlock,
    ToolResult,
    ToolTrace,
)
from app.text.utils import lexical_overlap
from app.tool.registry import ToolRegistry


THOUGHT_BLOCK_PATTERN = re.compile(r"<thought>.*?</thought>", re.IGNORECASE | re.DOTALL)
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class AgentEnvelope(BaseModel):
    status: str
    answer: str
    limitations: list[str] = Field(default_factory=list)
    clarification: ClarificationPayload | None = None


class AgentRun(AgentEnvelope):
    thoughts: list[str] = Field(default_factory=list)
    debug: AgentDebugPayload | None = None
    tool_trace: list[ToolTrace] = Field(default_factory=list)
    resolved_items: list[NormalizedEvidence] = Field(default_factory=list)
    plan_status: PlanStatus | None = None


class ValidatorEnvelope(BaseModel):
    expected_rows: int | None = None
    actual_rows: int | None = None
    findings: list[str] = Field(default_factory=list)
    ambiguity: list[str] = Field(default_factory=list)
    missing_statistics: list[str] = Field(default_factory=list)
    confidence: float | None = None
    normalized_rows: list[dict[str, Any]] = Field(default_factory=list)
    normalized_evidence: list[dict[str, Any]] = Field(default_factory=list)
    aggregates: dict[str, Any] = Field(default_factory=dict)


class AgentEngine:
    # Motivation vs Logic: this engine now runs an explicit planner -> retrieval
    # -> validator -> composer lifecycle so every answer is grounded in a
    # persisted plan, step-by-step TODO progress, and memoized evidence cache.
    def __init__(self, settings: Settings, tool_registry: ToolRegistry, logger: logging.Logger) -> None:
        self.settings = settings
        self.tool_registry = tool_registry
        self.logger = logger
        self._client: httpx.AsyncClient | None = None
        if self.settings.has_foundry:
            self._client = httpx.AsyncClient(
                base_url=f"{self.settings.foundry_endpoint.rstrip('/')}/openai/v1",
                headers={"api-key": self.settings.foundry_api_key or "", "Content-Type": "application/json"},
                timeout=self.settings.foundry_timeout_seconds,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def run(self, request: AgentQueryRequest, session_state: SessionState) -> AgentRun:
        if not self.settings.has_foundry:
            return AgentRun(
                status="error",
                answer="Azure AI Foundry is not configured, so the agent cannot plan or answer `/query` requests yet.",
                limitations=["Missing AZURE_AI_FOUNDRY_ENDPOINT or AZURE_AI_FOUNDRY_API_KEY."],
            )

        thoughts: list[str] = []
        traces: list[ToolTrace] = []
        resolved_items: list[NormalizedEvidence] = []
        limitations: list[str] = []
        clarification: ClarificationPayload | None = None
        status_hint = "answered"
        inventory_snapshot: dict[str, Any] | None = None
        parallel_batches: list[AgentDebugParallelBatch] = []
        used_grounded_snapshot_fallback = False
        used_snapshot_fast_path = False
        phase_timings_ms: dict[str, int] = {
            "planner": 0,
            "retrieval": 0,
            "validator": 0,
            "composer": 0,
            "formatter": 0,
        }
        run_started_at = perf_counter()

        planner_started_at = perf_counter()
        plan_status, planning_limitations = await self.plan_query(request.message, session_state)
        phase_timings_ms["planner"] = int((perf_counter() - planner_started_at) * 1000)
        limitations.extend(planning_limitations)
        self._persist_plan_state(session_state, plan_status)

        plan_snapshot = json.dumps(plan_status.model_dump(mode="json"), ensure_ascii=False)
        thoughts.append(plan_snapshot)

        messages = self._build_runtime_messages(
            request=request.message,
            session_state=session_state,
            intent_classes=plan_status.intent_classes,
            context_mode="normal",
        )

        draft_answer = ""
        retrieval_started_at = perf_counter()
        for _ in range(self.settings.agent_max_steps):
            if self._all_plan_steps_done(plan_status):
                break

            if len(messages) == 2:
                response = await self._retry_on_context_limit(
                    "initial query completion",
                    lambda mode: self._complete(
                        messages=self._build_runtime_messages(
                            request=request.message,
                            session_state=session_state,
                            intent_classes=plan_status.intent_classes,
                            context_mode=mode,
                        ),
                        enable_tools=True,
                    ),
                )
            else:
                response = await self._complete(messages=messages, enable_tools=True)
            assistant = response["choices"][0]["message"]
            content = assistant.get("content")
            if content:
                thoughts.append(content.strip())

            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                candidate_answer = self._extract_user_facing_answer(content)
                if self._all_plan_steps_done(plan_status):
                    draft_answer = candidate_answer
                    if not draft_answer:
                        limitations.append("The model returned an empty final assistant message after planned retrieval.")
                        status_hint = "limited"
                    break

                next_step = self._next_open_step(plan_status)
                if next_step is None:
                    limitations.append("The model returned an empty or premature final assistant message before plan completion.")
                    limitations.append(self._plan_incomplete_note(next_step))
                    draft_answer = self._plan_incomplete_answer(next_step)
                    status_hint = "limited"
                    break

                resolved_args, resolution_note = self._resolve_planned_step_args(next_step, session_state)
                if resolved_args is None:
                    # Root Cause vs Logic: auto-executing incomplete lookup steps
                    # (for both product and variant evidence tools) leaked raw
                    # schema validation errors into user-visible limitations. We
                    # now stop earlier and request the missing identifier safely.
                    clarification_question = self._clarification_question_for_step(next_step)
                    clarification = ClarificationPayload(
                        question=clarification_question,
                        options=[],
                    )
                    if resolution_note:
                        limitations.append(resolution_note)
                    draft_answer = clarification_question
                    status_hint = "needs_clarification"
                    plan_status.status = "needs_clarification"
                    break

                next_step.args = resolved_args
                if resolution_note:
                    limitations.append(resolution_note)
                limitations.append(
                    f"The model skipped tool selection, so the runtime executed planned step `{next_step.id}` directly."
                )
                tool_calls = [
                    {
                        "id": f"planned_step_{next_step.id}",
                        "type": "function",
                        "function": {
                            "name": next_step.tool,
                            "arguments": json.dumps(resolved_args, ensure_ascii=False),
                        },
                    }
                ]
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant.get("content") or self._direct_step_content(next_step),
                        "tool_calls": tool_calls,
                    }
                )
            else:
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant.get("content"),
                        "tool_calls": tool_calls,
                    }
                )

            prepared_calls: list[dict[str, Any]] = []
            assistant_thought = (assistant.get("content") or "").strip()
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                raw_arguments = tool_call["function"].get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    parsed_args = {"_raw": raw_arguments}
                normalized_args = parsed_args if isinstance(parsed_args, dict) else {"value": parsed_args}

                plan_step, inserted = self._resolve_or_insert_plan_step(plan_status, tool_name, normalized_args)
                plan_step.status = "in-progress"
                prepared_calls.append(
                    {
                        "tool_call": tool_call,
                        "tool_name": tool_name,
                        "normalized_args": normalized_args,
                        "plan_step": plan_step,
                        "inserted": inserted,
                    }
                )

            self._persist_plan_state(session_state, plan_status)

            execute_in_parallel = self._should_execute_parallel(prepared_calls)
            if len(prepared_calls) > 1:
                parallel_batches.append(
                    AgentDebugParallelBatch(
                        batch_id=len(parallel_batches) + 1,
                        execution_mode="parallel" if execute_in_parallel else "sequential",
                        tools=[item["tool_name"] for item in prepared_calls],
                        step_ids=[item["plan_step"].id for item in prepared_calls],
                    )
                )

            raw_outcomes: list[ToolResult | Exception] = []
            if execute_in_parallel:
                raw_outcomes = [RuntimeError("tool batch not started")] * len(prepared_calls)

                async def run_prepared_call(index: int, item: dict[str, Any]) -> None:
                    try:
                        raw_outcomes[index] = await self.tool_registry.call_tool(
                            item["tool_name"],
                            item["normalized_args"],
                            session_id=session_state.session_id,
                            thought=assistant_thought,
                        )
                    except Exception as exc:  # pragma: no cover - mirrored error flow
                        raw_outcomes[index] = exc

                async with anyio.create_task_group() as task_group:
                    for index, item in enumerate(prepared_calls):
                        task_group.start_soon(run_prepared_call, index, item)
            else:
                for item in prepared_calls:
                    try:
                        raw_outcomes.append(
                            await self.tool_registry.call_tool(
                                item["tool_name"],
                                item["normalized_args"],
                                session_id=session_state.session_id,
                                thought=assistant_thought,
                            )
                        )
                    except Exception as exc:  # pragma: no cover - mirrored error flow
                        raw_outcomes.append(exc)

            supported_tool_errors = (InventoryNotFoundError, ParameterMappingError, UpstreamServiceError, ValueError)
            tool_messages: list[dict[str, Any]] = []
            for prepared, outcome in zip(prepared_calls, raw_outcomes):
                tool_call = prepared["tool_call"]
                tool_name = prepared["tool_name"]
                normalized_args = prepared["normalized_args"]
                plan_step = prepared["plan_step"]
                if prepared["inserted"]:
                    limitations.append(
                        f"Plan step was added at runtime for `{tool_name}` so retrieval stayed explicit before execution."
                    )

                try:
                    if isinstance(outcome, Exception):
                        if not isinstance(outcome, supported_tool_errors):
                            raise outcome
                        raise outcome

                    result = outcome
                    if result.tool == "stock.inventory_snapshot" and isinstance(result.llm_content, dict):
                        inventory_snapshot = result.llm_content
                    if result.trace:
                        traces.append(result.trace)

                    new_limitations, new_clarification, new_evidence = self._capture(result.data)
                    limitations.extend(new_limitations)
                    if new_clarification is not None:
                        clarification = new_clarification
                        session_state.last_candidate_list = new_clarification.options
                        status_hint = "needs_clarification"
                        plan_status.status = "needs_clarification"
                    if new_evidence:
                        for evidence in new_evidence:
                            self._update_session_with_evidence(session_state, evidence)
                        resolved_items = self._merge_evidence(resolved_items, new_evidence)

                    validator_started_at = perf_counter()
                    validation, memo_update, validation_limitations = await self.validate_and_record(
                        session_state=session_state,
                        plan_status=plan_status,
                        step=plan_step,
                        tool_name=tool_name,
                        tool_args=normalized_args,
                        result=result,
                    )
                    phase_timings_ms["validator"] += int((perf_counter() - validator_started_at) * 1000)
                    limitations.extend(validation_limitations)
                    plan_step.validation = validation
                    plan_step.status = "done"

                    result.plan_status = plan_status
                    result.memo_update = memo_update
                    result.validation = validation

                    follow_up_note = self._append_recursive_follow_up(
                        request_message=request.message,
                        plan_status=plan_status,
                        completed_step=plan_step,
                        result=result,
                    )
                    if follow_up_note:
                        limitations.append(follow_up_note)
                    resolver_follow_up = self._append_resolver_follow_up(
                        plan_status=plan_status,
                        completed_step=plan_step,
                        result=result,
                    )
                    if resolver_follow_up:
                        limitations.append(resolver_follow_up)
                    tool_content = self._render_tool_content(result)
                except supported_tool_errors as exc:
                    error_trace = ToolTrace(
                        thought=assistant_thought,
                        tool=tool_name,
                        args=normalized_args,
                        status="error",
                        normalization_notes=[str(exc)],
                    )
                    traces.append(error_trace)
                    limitations.append(str(exc))

                    validation, memo_update = self._record_failed_step(
                        session_state=session_state,
                        plan_status=plan_status,
                        step=plan_step,
                        tool_name=tool_name,
                        tool_args=normalized_args,
                        error_message=str(exc),
                    )
                    plan_step.validation = validation
                    plan_step.status = "done"

                    tool_content = json.dumps(
                        {
                            "error": {
                                "type": exc.__class__.__name__,
                                "message": str(exc),
                                **({"status_code": exc.status_code} if isinstance(exc, UpstreamServiceError) else {}),
                            }
                        },
                        ensure_ascii=False,
                    )
                finally:
                    self._persist_plan_state(session_state, plan_status)

                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_content,
                    }
                )

            messages.extend(tool_messages)
        else:
            draft_answer = "I reached the tool-step limit before I could finish safely."
            limitations.append("The agent hit the configured max-step limit.")
            status_hint = "limited"
            plan_status.status = "blocked"
        phase_timings_ms["retrieval"] = int((perf_counter() - retrieval_started_at) * 1000)

        plan_complete = self._all_plan_steps_done(plan_status)
        if clarification is not None:
            status_hint = "needs_clarification"
            plan_status.status = "needs_clarification"
        elif plan_complete:
            plan_status.status = "complete"
        elif plan_status.status not in {"blocked", "error"}:
            plan_status.status = "in-progress"

        if clarification is None and not plan_complete:
            status_hint = "limited"
            next_step = self._next_open_step(plan_status)
            limitations.append(self._plan_incomplete_note(next_step))
            draft_answer = draft_answer or self._plan_incomplete_answer(next_step)

        if clarification is None and plan_complete:
            if inventory_snapshot:
                coverage = inventory_snapshot.get("coverage") or {}
                draft_answer = render_inventory_snapshot_markdown(
                    rows=inventory_snapshot.get("rows", []),
                    coverage=coverage,
                )
                draft_answer = (
                    f"{draft_answer}\n\n"
                    "If you want, I can drill into one specific variant with a deeper breakdown."
                )
                status_hint = "limited" if coverage.get("isPartial") or coverage.get("limitations") else "answered"
                used_snapshot_fast_path = True
            else:
                composer_started_at = perf_counter()
                composed_answer, compose_limitations = await self._compose_answer_from_plan(
                    request=request.message,
                    plan_status=plan_status,
                    session_state=session_state,
                    limitations=limitations,
                    include_thoughts=request.includeThoughts,
                    thoughts=thoughts,
                )
                phase_timings_ms["composer"] = int((perf_counter() - composer_started_at) * 1000)
                limitations.extend(compose_limitations)
                if composed_answer:
                    draft_answer = composed_answer

        # Root Cause vs Logic: the model sometimes returns its last turn with
        # no tool_calls but also empty `content`, which left `draft_answer` blank
        # and forced a generic fallback. A single no-tools completion asks the
        # model to synthesize from the thread (still LLM-orchestrated, no keyword routing).
        if not (draft_answer or "").strip() and (traces or resolved_items) and self._client:
            draft_answer, synthesis_limitations = await self._synthesize_answer(
                messages=messages,
                include_thoughts=request.includeThoughts,
                thoughts=thoughts,
            )
            limitations.extend(synthesis_limitations)
            if draft_answer:
                if status_hint != "needs_clarification":
                    status_hint = "answered"
            else:
                fallback_answer, fallback_status, fallback_limitations = self._grounded_fallback_answer(
                    clarification=clarification,
                    inventory_snapshot=inventory_snapshot,
                )
                draft_answer = fallback_answer
                status_hint = fallback_status
                limitations.extend(fallback_limitations)
                used_grounded_snapshot_fallback = bool(inventory_snapshot and not clarification)

        if used_grounded_snapshot_fallback or used_snapshot_fast_path:
            envelope = AgentEnvelope(
                status=status_hint,
                answer=draft_answer,
                limitations=self._dedupe(limitations),
                clarification=clarification,
            )
        else:
            try:
                formatter_started_at = perf_counter()
                envelope = await self._format(
                    request=request.message,
                    draft=draft_answer or self._default_incomplete_answer(clarification is not None),
                    limitations=limitations,
                    clarification=clarification,
                    fallback_status=status_hint,
                )
                phase_timings_ms["formatter"] = int((perf_counter() - formatter_started_at) * 1000)
            except UpstreamServiceError as exc:
                # Root Cause vs Logic: formatting is a polish pass, not source-of-truth
                # retrieval. If it times out we should still return the grounded draft
                # and truthful status instead of turning a successful tool run into a 5xx.
                self.logger.warning("Agent formatter pass failed: %s", exc)
                limitations.append(str(exc))
                envelope = AgentEnvelope(
                    status=status_hint,
                    answer=draft_answer or self._default_incomplete_answer(clarification is not None),
                    limitations=self._dedupe(limitations),
                    clarification=clarification,
                )

        total_elapsed_ms = int((perf_counter() - run_started_at) * 1000)
        self.logger.info(
            "query_phase_timings session_id=%s planner_ms=%s retrieval_ms=%s validator_ms=%s composer_ms=%s formatter_ms=%s total_ms=%s",
            session_state.session_id,
            phase_timings_ms["planner"],
            phase_timings_ms["retrieval"],
            phase_timings_ms["validator"],
            phase_timings_ms["composer"],
            phase_timings_ms["formatter"],
            total_elapsed_ms,
        )

        self._persist_plan_state(session_state, plan_status)
        debug_payload = (
            self._build_debug_payload(
                request=request.message,
                plan_status=plan_status,
                session_state=session_state,
                thoughts=thoughts,
                traces=traces,
                resolved_items=resolved_items,
                limitations=self._dedupe(limitations + envelope.limitations),
                parallel_batches=parallel_batches,
            )
            if request.includeThoughts
            else None
        )
        return AgentRun(
            status=envelope.status,
            answer=envelope.answer,
            limitations=self._dedupe(limitations + envelope.limitations),
            clarification=envelope.clarification or clarification,
            thoughts=thoughts if request.includeThoughts else [],
            debug=debug_payload,
            tool_trace=traces,
            resolved_items=resolved_items,
            plan_status=plan_status,
        )

    async def plan_query(self, request: str, session_state: SessionState) -> tuple[PlanStatus, list[str]]:
        limitations: list[str] = []

        if session_state.current_plan and session_state.current_plan.status == "in-progress":
            resumed = session_state.current_plan.model_copy(deep=True)
            if not resumed.memo.entries and session_state.memo_cache.entries:
                resumed.memo = session_state.memo_cache.model_copy(deep=True)
            return resumed, []

        if self._client is None:
            limitations.append("Planner model is unavailable, so a minimal fallback plan was created.")
            return self._fallback_plan(request, session_state), limitations

        try:
            async def run_mode(context_mode: str) -> PlanStatus:
                payload = {
                    "model": self.settings.foundry_model,
                    "messages": [
                        {"role": "system", "content": "You return strict JSON planning objects for tool orchestration."},
                        {
                            "role": "user",
                            "content": render_planner(
                                request=request,
                                session=session_state,
                                tools=self.tool_registry.list_tools(),
                                context_mode=context_mode,
                            ),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                    "max_completion_tokens": 1200,
                }
                response_payload = await self._post_chat_completion(payload, endpoint_name="/api/v1/query/planner")
                content = response_payload["choices"][0]["message"].get("content", "")
                raw = json.loads(content)
                return self._sanitize_plan(raw, request, session_state)

            plan = await self._retry_on_context_limit("planner", run_mode)
            return plan, limitations
        except (UpstreamServiceError, json.JSONDecodeError, ValidationError) as exc:
            limitations.append(f"Planner pass failed; fallback plan was used. reason={exc}")
            return self._fallback_plan(request, session_state), limitations

    async def validate_and_record(
        self,
        *,
        session_state: SessionState,
        plan_status: PlanStatus,
        step: PlanStep,
        tool_name: str,
        tool_args: dict[str, Any],
        result: ToolResult,
    ) -> tuple[PlanValidation, MemoEntry, list[str]]:
        # Motivation vs Logic: every retrieval now writes normalized rows,
        # evidence, and validation findings into a shared memo cache so follow-up
        # steps reuse grounded data instead of re-deriving from raw tool payloads.
        limitations: list[str] = []
        validator = await self._validator_envelope(
            plan_status=plan_status,
            step=step,
            tool_name=tool_name,
            tool_args=tool_args,
            result=result,
            memo_cache=session_state.memo_cache,
        )

        actual_rows = validator.actual_rows
        if actual_rows is None:
            actual_rows = self._count_rows(result.data, result.trace)

        expected_rows = validator.expected_rows
        if expected_rows is None:
            expected_rows = self._expected_rows(step, result.trace, actual_rows)

        findings = list(validator.findings)
        if expected_rows is not None and actual_rows is not None and expected_rows != actual_rows:
            findings.append(
                f"Expected {expected_rows} rows based on plan/cached context but retrieved {actual_rows}."
            )

        prior_match = [
            entry
            for entry in session_state.memo_cache.entries
            if entry.tool == tool_name and entry.args == tool_args and entry.rows
        ]
        if prior_match:
            previous_rows = len(prior_match[-1].rows)
            if previous_rows != actual_rows:
                findings.append(
                    f"Cached divergence detected: prior matching call had {previous_rows} rows, current call has {actual_rows}."
                )

        missing_statistics = list(validator.missing_statistics)
        if isinstance(result.data, dict):
            coverage = result.data.get("coverage")
            if isinstance(coverage, dict):
                for limitation in coverage.get("limitations", []):
                    if limitation:
                        missing_statistics.append(str(limitation))

        validation = PlanValidation(
            expected_rows=expected_rows,
            actual_rows=actual_rows,
            cache_status=result.trace.cache_status if result.trace else None,
            findings=self._dedupe(findings),
            ambiguity=self._dedupe(validator.ambiguity),
            missing_statistics=self._dedupe(missing_statistics),
            confidence=validator.confidence,
        )

        normalized_rows = validator.normalized_rows or self._fallback_rows(result.data)
        normalized_evidence = validator.normalized_evidence or self._fallback_evidence(result.data)

        memo_entry = MemoEntry(
            step_id=step.id,
            tool=tool_name,
            args=tool_args,
            rows=normalized_rows,
            evidence=normalized_evidence,
            aggregates=validator.aggregates,
            provenance={
                "tool": tool_name,
                "args": tool_args,
                "cache_status": result.trace.cache_status if result.trace else None,
                "source_data": result.trace.source_data if result.trace else None,
                "captured_at": self._utc_now_iso(),
            },
        )

        session_state.memo_cache.entries.append(memo_entry)
        session_state.memo_cache.aggregates = self._recompute_memo_aggregates(session_state)
        plan_status.memo = session_state.memo_cache

        self._update_plan_metadata(session_state, plan_status, step.id, validation)

        if not self.settings.has_foundry:
            limitations.append("Validator model is unavailable, so deterministic normalization was used.")

        return validation, memo_entry, limitations

    def _record_failed_step(
        self,
        *,
        session_state: SessionState,
        plan_status: PlanStatus,
        step: PlanStep,
        tool_name: str,
        tool_args: dict[str, Any],
        error_message: str,
    ) -> tuple[PlanValidation, MemoEntry]:
        validation = PlanValidation(
            expected_rows=self._expected_rows(step, None, 0),
            actual_rows=0,
            cache_status="error",
            findings=[error_message],
            ambiguity=[],
            missing_statistics=[],
            confidence=0.0,
        )

        memo_entry = MemoEntry(
            step_id=step.id,
            tool=tool_name,
            args=tool_args,
            rows=[],
            evidence=[],
            aggregates={},
            provenance={
                "tool": tool_name,
                "args": tool_args,
                "error": error_message,
                "captured_at": self._utc_now_iso(),
            },
        )
        session_state.memo_cache.entries.append(memo_entry)
        session_state.memo_cache.aggregates = self._recompute_memo_aggregates(session_state)
        plan_status.memo = session_state.memo_cache
        self._update_plan_metadata(session_state, plan_status, step.id, validation)
        return validation, memo_entry

    async def _validator_envelope(
        self,
        *,
        plan_status: PlanStatus,
        step: PlanStep,
        tool_name: str,
        tool_args: dict[str, Any],
        result: ToolResult,
        memo_cache,
    ) -> ValidatorEnvelope:
        if self._client is None:
            return self._fallback_validator_envelope(result)

        try:
            trace_payload = result.trace.model_dump(mode="json") if result.trace else {}
            tool_payload = result.llm_content if result.llm_content is not None else result.data

            async def run_mode(context_mode: str) -> ValidatorEnvelope:
                payload = {
                    "model": self.settings.foundry_model,
                    "messages": [
                        {"role": "system", "content": "You return strict JSON validation objects for retrieval outputs."},
                        {
                            "role": "user",
                            "content": render_validator(
                                plan=plan_status,
                                step=step,
                                tool_name=tool_name,
                                tool_args=tool_args,
                                tool_result=tool_payload,
                                tool_trace=trace_payload,
                                memo_cache=memo_cache,
                                context_mode=context_mode,
                            ),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                    "max_completion_tokens": 1400,
                }
                response_payload = await self._post_chat_completion(payload, endpoint_name="/api/v1/query/validator")
                content = response_payload["choices"][0]["message"].get("content", "")
                raw = json.loads(content)
                return ValidatorEnvelope.model_validate(raw)

            return await self._retry_on_context_limit("validator", run_mode)
        except (UpstreamServiceError, json.JSONDecodeError, ValidationError) as exc:
            self.logger.warning("Validator pass failed, using deterministic fallback: %s", exc)
            fallback = self._fallback_validator_envelope(result)
            fallback.findings.append("Validator model output was invalid; deterministic fallback normalization was used.")
            return fallback

    def _fallback_validator_envelope(self, result: ToolResult) -> ValidatorEnvelope:
        return ValidatorEnvelope(
            expected_rows=None,
            actual_rows=self._count_rows(result.data, result.trace),
            findings=[],
            ambiguity=[],
            missing_statistics=[],
            confidence=None,
            normalized_rows=self._fallback_rows(result.data),
            normalized_evidence=self._fallback_evidence(result.data),
            aggregates={},
        )

    def _fallback_rows(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            rows = data.get("rows")
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def _fallback_evidence(self, data: Any) -> list[dict[str, Any]]:
        evidence: list[NormalizedEvidence] = []
        self._collect_evidence(data, evidence)
        return [item.model_dump(mode="json") for item in evidence]

    def _count_rows(self, data: Any, trace: ToolTrace | None) -> int:
        if trace and trace.result_count is not None:
            return max(trace.result_count, 0)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ["rows", "items", "articles", "sources", "locations", "points", "options"]:
                value = data.get(key)
                if isinstance(value, list):
                    return len(value)
            forecast = data.get("forecast")
            if isinstance(forecast, dict) and isinstance(forecast.get("list"), list):
                return len(forecast.get("list", []))
            rates = data.get("rates")
            if isinstance(rates, dict):
                return len(rates)
            return 1 if data else 0
        return 0

    def _expected_rows(self, step: PlanStep, trace: ToolTrace | None, actual_rows: int) -> int:
        args = step.args or {}
        page_size = args.get("pageSize")
        if isinstance(page_size, int) and page_size > 0:
            if trace and trace.result_count is not None and trace.result_count < page_size:
                return trace.result_count
            return page_size
        if trace and trace.result_count is not None:
            return trace.result_count
        return actual_rows

    def _should_execute_parallel(self, prepared_calls: list[dict[str, Any]]) -> bool:
        if len(prepared_calls) < 2:
            return False
        return not any(str(item["tool_name"]).startswith("session.") for item in prepared_calls)

    def _append_recursive_follow_up(
        self,
        *,
        request_message: str,
        plan_status: PlanStatus,
        completed_step: PlanStep,
        result: ToolResult,
    ) -> str | None:
        if result.tool != "stock.search_catalogue":
            return None
        if completed_step.name == "recursive detail retrieval":
            return None
        if any(
            step.status != "done" and step.tool in {"stock.get_product", "stock.extract_variant_evidence"}
            for step in plan_status.steps
        ):
            return None
        if any(step.status != "done" and step.name == "recursive detail retrieval" for step in plan_status.steps):
            return None

        follow_up_steps = self._derive_follow_up_steps(
            result.data,
            request_message=request_message,
        )
        if not follow_up_steps:
            return None

        inserted_count = 0
        for follow_up_tool, follow_up_args in follow_up_steps:
            if any(
                step.tool == follow_up_tool and step.args == follow_up_args and step.status != "done"
                for step in plan_status.steps
            ):
                continue

            next_id = max((step.id for step in plan_status.steps), default=0) + 1
            plan_status.steps.append(
                PlanStep(
                    id=next_id,
                    name="recursive detail retrieval",
                    tool=follow_up_tool,
                    status="pending",
                    args=follow_up_args,
                    depends_on=[completed_step.id],
                    parallel_group=None,
                    hypotheses=[
                        "The initial catalogue search resolved identifiers but not enough user-facing detail, so a follow-up retrieval hop is required."
                    ],
                    validation=None,
                )
            )
            inserted_count += 1
        if inserted_count == 0:
            return None
        return (
            f"Added `{inserted_count}` recursive detail retrieval step(s) because catalogue results were too thin "
            "to answer requested attributes safely."
        )

    def _derive_follow_up_steps(
        self,
        data: Any,
        request_message: str | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return []

        item = self._best_follow_up_candidate(items, request_message)
        if isinstance(item, dict):
            product_id = self._compact_identifier(item.get("id"))
            if product_id:
                return [("stock.get_product", {"id": product_id, "page": 1, "pageSize": 50})]

        # Motivation vs Logic: when candidate ranking cannot safely pick one item,
        # schedule bounded variant-evidence retrieval across returned rows so the
        # prompt-driven composer can still reason over concrete product evidence.
        variant_steps = self._derive_variant_follow_up_steps(items, max_variants=6)
        if variant_steps:
            return variant_steps

        if not isinstance(item, dict):
            return []
        product_id = self._compact_identifier(item.get("id"))
        variants = item.get("variants")
        variant = variants[0] if isinstance(variants, list) and len(variants) == 1 and isinstance(variants[0], dict) else None
        sku = self._compact_identifier(variant.get("sku")) if isinstance(variant, dict) else None
        if sku:
            return [("stock.get_product", {"sku": sku})]
        if product_id:
            return [("stock.get_product", {"id": product_id})]
        return []

    def _derive_variant_follow_up_steps(self, items: list[Any], max_variants: int = 20) -> list[tuple[str, dict[str, str]]]:
        steps: list[tuple[str, dict[str, str]]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            product_id = self._compact_identifier(item.get("id"))
            variants = item.get("variants")
            if not isinstance(variants, list):
                continue
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                variant_id = self._compact_identifier(variant.get("id"))
                sku = self._compact_identifier(variant.get("sku"))
                lookup = self._compose_variant_lookup(sku=sku, product_id=product_id, variant_id=variant_id)
                if lookup is None:
                    continue
                dedupe_key = (
                    lookup.get("sku", ""),
                    lookup.get("variantId", lookup.get("id", "")),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                steps.append(("stock.extract_variant_evidence", lookup))
                if len(steps) >= max_variants:
                    return steps
        return steps

    def _append_resolver_follow_up(
        self,
        *,
        plan_status: PlanStatus,
        completed_step: PlanStep,
        result: ToolResult,
    ) -> str | None:
        if result.tool != "resolver.disambiguate_candidates":
            return None
        if not isinstance(result.data, dict):
            return None
        if result.data.get("status") != "resolved_product_family":
            return None

        product_id = self._compact_identifier(result.data.get("product_id"))
        if not product_id:
            return None
        follow_up_tool = "stock.get_product"
        follow_up_args: dict[str, Any] = {"id": product_id, "page": 1, "pageSize": 50}
        if any(
            step.tool == follow_up_tool and step.args == follow_up_args and step.status != "done"
            for step in plan_status.steps
        ):
            return None

        next_id = max((step.id for step in plan_status.steps), default=0) + 1
        plan_status.steps.append(
            PlanStep(
                id=next_id,
                name="resolved family detail retrieval",
                tool=follow_up_tool,
                status="pending",
                args=follow_up_args,
                depends_on=[completed_step.id],
                parallel_group=None,
                hypotheses=[
                    "Resolver narrowed ambiguity to one product family, so retrieve full product details for all variants."
                ],
                validation=None,
            )
        )
        return (
            f"Resolved product family follow-up step `{next_id}` was added for `{follow_up_tool}` "
            "so the final answer includes all variants before any clarification prompt."
        )

    def _best_follow_up_candidate(self, items: list[Any], request_message: str | None) -> dict[str, Any] | None:
        dict_items = [item for item in items if isinstance(item, dict)]
        if not dict_items:
            return None
        if len(dict_items) == 1 or not request_message:
            return dict_items[0]

        scored: list[tuple[float, dict[str, Any]]] = []
        for item in dict_items:
            parts = [str(item.get("name") or "")]
            variants = item.get("variants")
            if isinstance(variants, list):
                parts.extend(str(variant.get("name") or "") for variant in variants if isinstance(variant, dict))
            candidate_text = " ".join(part for part in parts if part)
            scored.append((lexical_overlap(request_message, candidate_text), item))

        scored.sort(key=lambda entry: entry[0], reverse=True)
        top_score = scored[0][0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if top_score <= 0.0:
            return None
        if len(scored) > 1 and top_score < 0.55 and top_score - second_score < 0.15:
            return None
        return scored[0][1]

    def _resolve_or_insert_plan_step(
        self,
        plan_status: PlanStatus,
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[PlanStep, bool]:
        for step in plan_status.steps:
            if step.tool == tool_name and step.status != "done":
                if args:
                    step.args = args
                return step, False

        next_id = max((step.id for step in plan_status.steps), default=0) + 1
        inserted = PlanStep(
            id=next_id,
            name=f"runtime step {next_id}",
            tool=tool_name,
            status="pending",
            args=args,
            depends_on=[],
            parallel_group=None,
            hypotheses=["Tool was required by runtime retrieval before the plan listed it explicitly."],
            validation=None,
        )
        plan_status.steps.append(inserted)
        return inserted, True

    def _all_plan_steps_done(self, plan_status: PlanStatus) -> bool:
        return bool(plan_status.steps) and all(step.status == "done" for step in plan_status.steps)

    def _next_open_step(self, plan_status: PlanStatus) -> PlanStep | None:
        completed = {step.id for step in plan_status.steps if step.status == "done"}
        for step in plan_status.steps:
            if step.status != "done" and all(dep in completed for dep in step.depends_on):
                return step
        for step in plan_status.steps:
            if step.status != "done":
                return step
        return None

    def _plan_incomplete_note(self, step: PlanStep | None) -> str:
        if step is None:
            return "The plan is still incomplete because no executable step was resolved."
        return (
            f"Planned retrieval+validation is incomplete; next required step is "
            f"`plan.step.{step.id}` via `{step.tool}`."
        )

    def _plan_incomplete_answer(self, step: PlanStep | None) -> str:
        if step is None:
            return (
                "I couldn't finish this accurately with the available evidence. "
                "Please retry or narrow what you want me to list."
            )
        return (
            "I need one more pass to verify this before I can answer reliably. "
            "Please confirm the exact product or variant you want."
        )

    def _direct_step_content(self, step: PlanStep) -> str:
        return ThoughtBlock(
            goal=f"Execute planned retrieval step {step.id}",
            entity_guess="unknown",
            strategy="exact lookup",
            tool=step.tool,
            args_draft=step.args,
            risk="none",
        ).to_xml()

    def _clarification_question_for_step(self, step: PlanStep) -> str:
        if step.tool in {"stock.extract_variant_evidence", "stock.get_variant_evidence"}:
            return "Please share the exact variant SKU (or product ID plus variant ID) so I can continue safely."
        return "Please share the exact product SKU or product ID so I can continue safely."

    def _resolve_planned_step_args(
        self,
        step: PlanStep,
        session_state: SessionState,
    ) -> tuple[dict[str, Any] | None, str | None]:
        args = dict(step.args or {})
        if step.tool == "stock.get_product":
            return self._resolve_product_lookup_args(step_id=step.id, args=args, session_state=session_state)
        if step.tool in {"stock.extract_variant_evidence", "stock.get_variant_evidence"}:
            return self._resolve_variant_lookup_args(step_id=step.id, args=args, session_state=session_state)
        return args, None

    def _resolve_product_lookup_args(
        self,
        *,
        step_id: int,
        args: dict[str, Any],
        session_state: SessionState,
    ) -> tuple[dict[str, Any] | None, str | None]:
        compact_id = self._compact_identifier(args.get("id"))
        compact_sku = self._compact_identifier(args.get("sku"))
        if compact_id or compact_sku:
            normalized = dict(args)
            normalized["id"] = compact_id
            normalized["sku"] = compact_sku
            return normalized, None

        recovered = self._recover_product_identifier(session_state)
        if recovered is None:
            return (
                None,
                (
                    f"Planned step `{step_id}` could not run because `stock.get_product` "
                    "requires an `id` or `sku`, and no reusable identifier was present in session evidence."
                ),
            )

        normalized = dict(args)
        normalized.update(recovered)
        return (
            normalized,
            (
                f"Runtime recovered missing lookup args for planned step `{step_id}` "
                f"from session evidence ({', '.join(recovered.keys())})."
            ),
        )

    def _resolve_variant_lookup_args(
        self,
        *,
        step_id: int,
        args: dict[str, Any],
        session_state: SessionState,
    ) -> tuple[dict[str, Any] | None, str | None]:
        compact_id = self._compact_identifier(args.get("id"))
        compact_sku = self._compact_identifier(args.get("sku"))
        compact_variant_id = self._compact_identifier(args.get("variantId"))

        normalized = dict(args)
        normalized["id"] = compact_id
        normalized["sku"] = compact_sku
        normalized["variantId"] = compact_variant_id

        if compact_id or compact_sku:
            return normalized, None

        if compact_variant_id:
            recovered = self._recover_product_identifier(session_state)
            if recovered is None:
                return (
                    None,
                    (
                        f"Planned step `{step_id}` had only `variantId`, but "
                        "`stock.extract_variant_evidence` also needs `sku` or `id` to resolve product details."
                    ),
                )
            normalized.update(recovered)
            return (
                normalized,
                (
                    f"Runtime supplemented planned step `{step_id}` variant lookup args "
                    f"from session evidence ({', '.join(recovered.keys())})."
                ),
            )

        recovered = self._recover_variant_lookup_identifier(session_state)
        if recovered is None:
            return (
                None,
                (
                    f"Planned step `{step_id}` could not run because "
                    "`stock.extract_variant_evidence` requires `id`, `sku`, or `variantId`, "
                    "and no reusable identifier was present in session evidence."
                ),
            )

        normalized.update(recovered)
        return (
            normalized,
            (
                f"Runtime recovered missing lookup args for planned step `{step_id}` "
                f"from session evidence ({', '.join(recovered.keys())})."
            ),
        )

    def _recover_variant_lookup_identifier(self, session_state: SessionState) -> dict[str, str] | None:
        for option in session_state.last_candidate_list:
            recovered = self._recover_variant_identifier_from_candidate_option(option)
            if recovered is not None:
                return recovered

        for entry in reversed(session_state.memo_cache.entries):
            recovered = self._recover_variant_identifier_from_memo_entry(entry)
            if recovered is not None:
                return recovered

        return self._recover_product_identifier(session_state)

    def _recover_variant_identifier_from_candidate_option(self, option: CandidateOption) -> dict[str, str] | None:
        sku = self._compact_identifier(option.sku)
        product_id = self._compact_identifier(option.product_id)
        variant_id = self._compact_identifier(option.variant_id)
        return self._compose_variant_lookup(sku=sku, product_id=product_id, variant_id=variant_id)

    def _recover_variant_identifier_from_memo_entry(self, entry: MemoEntry) -> dict[str, str] | None:
        sku = self._compact_identifier(entry.args.get("sku"))
        product_id = (
            self._compact_identifier(entry.args.get("id"))
            or self._compact_identifier(entry.args.get("product_id"))
            or self._compact_identifier(entry.args.get("productId"))
        )
        variant_id = (
            self._compact_identifier(entry.args.get("variantId"))
            or self._compact_identifier(entry.args.get("variant_id"))
        )
        recovered = self._compose_variant_lookup(sku=sku, product_id=product_id, variant_id=variant_id)
        if recovered is not None:
            return recovered

        for collection in [entry.evidence, entry.rows]:
            for item in collection:
                if not isinstance(item, dict):
                    continue
                recovered = self._compose_variant_lookup(
                    sku=self._compact_identifier(item.get("sku")),
                    product_id=(
                        self._compact_identifier(item.get("product_id"))
                        or self._compact_identifier(item.get("productId"))
                        or self._compact_identifier(item.get("id"))
                    ),
                    variant_id=(
                        self._compact_identifier(item.get("variant_id"))
                        or self._compact_identifier(item.get("variantId"))
                    ),
                )
                if recovered is not None:
                    return recovered

        return None

    def _compose_variant_lookup(
        self,
        *,
        sku: str | None,
        product_id: str | None,
        variant_id: str | None,
    ) -> dict[str, str] | None:
        if sku:
            payload = {"sku": sku}
            if variant_id:
                payload["variantId"] = variant_id
            return payload

        if product_id:
            payload = {"id": product_id}
            if variant_id:
                payload["variantId"] = variant_id
            return payload

        return None

    def _recover_product_identifier(self, session_state: SessionState) -> dict[str, str] | None:
        for identifier in session_state.recent_resolved_identifiers:
            compact = self._compact_identifier(identifier)
            if not compact:
                continue
            if self._looks_like_uuid(compact):
                return {"id": compact}
            return {"sku": compact}

        for option in session_state.last_candidate_list:
            if option.sku:
                compact = self._compact_identifier(option.sku)
                if compact:
                    return {"sku": compact}
            if option.product_id:
                compact = self._compact_identifier(option.product_id)
                if compact:
                    return {"id": compact}

        for entry in reversed(session_state.memo_cache.entries):
            recovered = self._recover_identifier_from_memo_entry(entry)
            if recovered is not None:
                return recovered

        return None

    def _recover_identifier_from_memo_entry(self, entry: MemoEntry) -> dict[str, str] | None:
        for key, target in [("sku", "sku"), ("id", "id"), ("product_id", "id"), ("productId", "id")]:
            candidate = self._compact_identifier(entry.args.get(key))
            if candidate:
                return {target: candidate}

        for collection in [entry.evidence, entry.rows]:
            for item in collection:
                if not isinstance(item, dict):
                    continue
                for key, target in [("sku", "sku"), ("product_id", "id"), ("productId", "id"), ("id", "id")]:
                    candidate = self._compact_identifier(item.get(key))
                    if candidate:
                        return {target: candidate}
        return None

    def _compact_identifier(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        compact = value.strip()
        return compact or None

    def _looks_like_uuid(self, value: str) -> bool:
        return bool(UUID_PATTERN.match(value))

    def _persist_plan_state(self, session_state: SessionState, plan_status: PlanStatus) -> None:
        plan_status.memo = session_state.memo_cache
        session_state.current_plan = plan_status
        session_state.plan_todo = [step.model_copy(deep=True) for step in plan_status.steps]
        session_state.plan_metadata.sorted_priorities = [
            step.id for step in plan_status.steps if step.status != "done"
        ] + [step.id for step in plan_status.steps if step.status == "done"]

    def persist_plan_state(self, session_state: SessionState, plan_status: PlanStatus) -> None:
        self._persist_plan_state(session_state, plan_status)

    def _update_plan_metadata(
        self,
        session_state: SessionState,
        plan_status: PlanStatus,
        step_id: int,
        validation: PlanValidation,
    ) -> None:
        score_key = f"plan.step.{step_id}"
        if validation.confidence is not None:
            session_state.plan_metadata.confidence_scores[score_key] = validation.confidence
        session_state.plan_metadata.validation_findings = self._dedupe(
            session_state.plan_metadata.validation_findings + validation.findings + validation.ambiguity
        )
        session_state.plan_metadata.sorted_priorities = [
            step.id for step in plan_status.steps if step.status != "done"
        ] + [step.id for step in plan_status.steps if step.status == "done"]

    def _recompute_memo_aggregates(self, session_state: SessionState) -> dict[str, Any]:
        tool_counts: dict[str, int] = {}
        stock_rankings: list[dict[str, Any]] = []

        for entry in session_state.memo_cache.entries:
            tool_counts[entry.tool] = tool_counts.get(entry.tool, 0) + 1
            for record in entry.evidence + entry.rows:
                total_stock = self._extract_total_stock(record)
                if total_stock is None:
                    continue
                stock_rankings.append(
                    {
                        "label": self._record_label(record),
                        "totalStock": total_stock,
                        "tool": entry.tool,
                        "step_id": entry.step_id,
                    }
                )

        top_ranked = sorted(stock_rankings, key=lambda item: item["totalStock"], reverse=True)[:5]
        return {
            "entry_count": len(session_state.memo_cache.entries),
            "tool_counts": tool_counts,
            "top_5_by_total_stock": top_ranked,
        }

    def _extract_total_stock(self, record: dict[str, Any]) -> int | None:
        direct = record.get("totalStock")
        if isinstance(direct, (int, float)):
            return int(direct)
        stock = record.get("stock")
        if isinstance(stock, dict):
            nested = stock.get("totalStock")
            if isinstance(nested, (int, float)):
                return int(nested)
        return None

    def _record_label(self, record: dict[str, Any]) -> str:
        for key in ["variant_name", "variant", "product_name", "product", "sku"]:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "unknown"

    def _sanitize_plan(self, raw: dict[str, Any], request: str, session_state: SessionState) -> PlanStatus:
        parsed = PlanStatus.model_validate(raw)
        allowed_tools = {tool.name for tool in self.tool_registry.list_tools()}

        sanitized_steps: list[PlanStep] = []
        id_mapping: dict[int, int] = {}
        next_id = 1
        for step in parsed.steps:
            if step.tool not in allowed_tools:
                continue
            id_mapping[step.id] = next_id
            next_id += 1

        next_id = 1
        for step in parsed.steps:
            if step.tool not in allowed_tools:
                continue
            status = step.status if step.status in {"planned", "pending", "in-progress", "done"} else "planned"
            depends_on = sorted(
                {
                    id_mapping[dependency]
                    for dependency in step.depends_on
                    if dependency in id_mapping and id_mapping[dependency] < id_mapping[step.id]
                }
            )
            sanitized_steps.append(
                PlanStep(
                    id=next_id,
                    name=step.name or f"step {next_id}",
                    tool=step.tool,
                    status=status,
                    args=step.args or {},
                    depends_on=depends_on,
                    parallel_group=step.parallel_group if isinstance(step.parallel_group, int) and step.parallel_group >= 0 else None,
                    hypotheses=step.hypotheses or [],
                    validation=step.validation,
                )
            )
            next_id += 1

        if not sanitized_steps:
            return self._fallback_plan(request, session_state)

        memo = session_state.memo_cache.model_copy(deep=True)
        if parsed.memo.entries:
            memo.entries.extend(parsed.memo.entries)
            memo.aggregates = parsed.memo.aggregates or memo.aggregates

        plan = PlanStatus(
            goal=parsed.goal or request,
            intent_classes=list(parsed.intent_classes),
            steps=sanitized_steps,
            memo=memo,
            status="in-progress",
        )
        return plan

    def _fallback_plan(self, request: str, session_state: SessionState) -> PlanStatus:
        tool_names = [tool.name for tool in self.tool_registry.list_tools()]
        default_tool = tool_names[0] if tool_names else "session.get_state"
        plan = PlanStatus(
            goal=request,
            steps=[
                PlanStep(
                    id=1,
                    name="initial retrieval",
                    tool=default_tool,
                    status="planned",
                    args={},
                    depends_on=[],
                    parallel_group=None,
                    hypotheses=["Fallback plan created because planner output was unavailable."],
                    validation=None,
                )
            ],
            memo=session_state.memo_cache.model_copy(deep=True),
            status="in-progress",
        )
        return plan

    async def _compose_answer_from_plan(
        self,
        *,
        request: str,
        plan_status: PlanStatus,
        session_state: SessionState,
        limitations: list[str],
        include_thoughts: bool,
        thoughts: list[str],
    ) -> tuple[str, list[str]]:
        if self._client is None:
            return "", ["Composer model is unavailable, so no final synthesis pass could run."]

        try:
            async def run_mode(context_mode: str) -> tuple[str, list[str]]:
                payload = {
                    "model": self.settings.foundry_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You write final user-facing answers grounded in retrieved evidence only.",
                        },
                        {
                            "role": "user",
                            "content": render_composer(
                                request=request,
                                plan=plan_status,
                                memo_cache=session_state.memo_cache,
                                limitations=self._composer_limitations(limitations),
                                context_mode=context_mode,
                            ),
                        },
                    ],
                    "max_completion_tokens": self.settings.agent_completion_tokens,
                }
                response_payload = await self._post_chat_completion(payload, endpoint_name="/api/v1/query/composer")
                assistant = response_payload["choices"][0]["message"]
                content = assistant.get("content")
                if content and include_thoughts:
                    thoughts.append(content.strip())

                if assistant.get("tool_calls"):
                    return "", ["Composer attempted tool calls; synthesis was blocked to keep final output grounded."]

                rendered = self._extract_user_facing_answer(content)
                if rendered:
                    return rendered, []

                return "", ["Composer pass returned empty content."]

            return await self._retry_on_context_limit("composer", run_mode)
        except UpstreamServiceError as exc:
            self.logger.warning("Composer pass failed: %s", exc)
            return "", [str(exc)]

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        enable_tools: bool = True,
    ) -> dict[str, Any]:
        if self._client is None:
            raise UpstreamServiceError(503, "Azure AI Foundry is not configured for `/api/v1/query`.")
        payload: dict[str, Any] = {
            "model": self.settings.foundry_model,
            "messages": messages,
            "max_completion_tokens": self.settings.agent_completion_tokens,
        }
        if enable_tools:
            payload["tools"] = self.tool_registry.tool_payloads()
            payload["tool_choice"] = "auto"
        return await self._post_chat_completion(payload, endpoint_name="/api/v1/query")

    def _build_runtime_messages(
        self,
        *,
        request: str,
        session_state: SessionState,
        intent_classes: list[str] | tuple[str, ...] | None = None,
        context_mode: str = "normal",
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": render_system(
                    request=request,
                    session=session_state,
                    tools=self.tool_registry.list_tools(),
                    intent_classes=intent_classes,
                    context_mode=context_mode,
                ),
            },
            {"role": "user", "content": request},
        ]

    async def _retry_on_context_limit(
        self,
        operation_name: str,
        builder: Callable[[str], Awaitable[Any]],
    ) -> Any:
        try:
            return await builder("normal")
        except UpstreamServiceError as exc:
            if not self._is_context_length_error(exc):
                raise
            # Root Cause vs Logic: the upstream `context_length_exceeded` error
            # means the prompt assembly is too large, so we retry with compacted
            # context instead of treating the visible chat as the culprit.
            self.logger.warning("Context length exceeded during %s; retrying with compact prompt.", operation_name)
            return await builder("compact")

    def _is_context_length_error(self, exc: UpstreamServiceError) -> bool:
        detail = exc.detail.lower()
        return "context_length_exceeded" in detail or "input tokens exceed" in detail or "messages resulted in" in detail

    async def complete_with_model(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_completion_tokens: int = 40,
    ) -> dict[str, Any]:
        if self._client is None:
            raise UpstreamServiceError(503, "Azure AI Foundry is not configured for `/api/v1/name-session`.")
        payload = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_completion_tokens,
        }
        return await self._post_chat_completion(payload, endpoint_name="/api/v1/name-session")

    async def _format(
        self,
        request: str,
        draft: str,
        limitations: list[str],
        clarification: ClarificationPayload | None,
        fallback_status: str,
    ) -> AgentEnvelope:
        if self._client is None:
            raise UpstreamServiceError(503, "Azure AI Foundry is not configured for `/api/v1/query`.")

        async def run_mode(_context_mode: str) -> AgentEnvelope:
            payload = {
                "model": self.settings.foundry_model,
                "messages": [
                    {"role": "system", "content": "You turn grounded tool outcomes into a strict JSON envelope."},
                    {
                        "role": "user",
                        "content": render_formatter(
                            request=request,
                            draft=draft,
                            limitations=self._dedupe(limitations),
                            clarification=clarification.model_dump(mode="json") if clarification else None,
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": 600,
            }
            response_payload = await self._post_chat_completion(payload, endpoint_name="/api/v1/query")
            content = response_payload["choices"][0]["message"]["content"]
            raw = json.loads(content)
            if clarification and raw.get("clarification") is None:
                raw["clarification"] = clarification.model_dump(mode="json")
            return AgentEnvelope.model_validate(raw)

        try:
            return await self._retry_on_context_limit("formatter", run_mode)
        except (json.JSONDecodeError, ValidationError):
            return AgentEnvelope(
                status=fallback_status,
                answer=draft,
                limitations=self._dedupe(limitations),
                clarification=clarification,
            )

    async def _post_chat_completion(self, payload: dict[str, Any], endpoint_name: str) -> dict[str, Any]:
        if self._client is None:
            raise UpstreamServiceError(503, "Azure AI Foundry is not configured.")
        # Root Cause vs Logic: Foundry read/network timeouts were escaping as raw
        # httpx exceptions and FastAPI returned 500. We normalize transport faults
        # into UpstreamServiceError so the API responds with stable 5xx semantics.
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.ReadTimeout as exc:
            raise UpstreamServiceError(
                504,
                f"Azure AI Foundry timed out while handling `{endpoint_name}`.",
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(
                502,
                f"Azure AI Foundry request failed while handling `{endpoint_name}`: {exc}",
            ) from exc

        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamServiceError(
                502,
                f"Azure AI Foundry returned non-JSON while handling `{endpoint_name}`.",
            ) from exc

    def _capture(
        self,
        data: Any,
    ) -> tuple[list[str], ClarificationPayload | None, list[NormalizedEvidence]]:
        limitations: list[str] = []
        clarification: ClarificationPayload | None = None
        evidence: list[NormalizedEvidence] = []

        if isinstance(data, dict) and data.get("status") == "needs_clarification":
            clarification = ClarificationPayload.model_validate(data)
            return limitations, clarification, evidence

        self._collect_evidence(data, evidence)

        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            message = error.get("message")
            if message:
                limitations.append(str(message))

        return limitations, clarification, evidence

    def _update_session_with_evidence(self, session_state: SessionState, evidence: NormalizedEvidence) -> None:
        if evidence.product_name:
            session_state.recent_product_names = self._append_unique(session_state.recent_product_names, evidence.product_name)
        if evidence.variant_name and evidence.variant_name != evidence.product_name:
            session_state.recent_product_names = self._append_unique(session_state.recent_product_names, evidence.variant_name)
        for identifier in [evidence.product_id, evidence.variant_id, evidence.sku]:
            if identifier:
                session_state.recent_resolved_identifiers = self._append_unique(
                    session_state.recent_resolved_identifiers,
                    identifier,
                )

    def _merge_evidence(
        self,
        existing: list[NormalizedEvidence],
        incoming: list[NormalizedEvidence],
    ) -> list[NormalizedEvidence]:
        seen = {item.variant_id or item.product_id or item.sku for item in existing}
        merged = list(existing)
        for item in incoming:
            identifier = item.variant_id or item.product_id or item.sku
            if identifier not in seen:
                merged.append(item)
                seen.add(identifier)
        return merged

    async def _synthesize_answer(
        self,
        messages: list[dict[str, Any]],
        include_thoughts: bool,
        thoughts: list[str],
    ) -> tuple[str, list[str]]:
        limitations: list[str] = []
        synthesis_messages = list(messages)
        prompts = [
            (
                "The tool outputs above are the authoritative inventory source for this run. "
                "Write the final user-facing answer now using only those tool results. Ground every "
                "claim in the returned JSON, use Markdown tables when listing many items, clearly "
                "state any coverage limits, group variants under a single product row label, rewrite "
                "raw stock metrics into plain language, and do not call tools or ask for new data sources."
            ),
            (
                "Your previous response did not contain a user-facing answer. Produce it now. "
                "If the retrieved evidence is partial, say that plainly and summarize only the "
                "retrieved rows. Keep wording non-technical and user-friendly, especially for stock "
                "availability summaries. Do not call tools. Do not ask for clarification unless a "
                "clarification payload already exists in the conversation."
            ),
        ]

        for prompt in prompts:
            synthesis_messages.append({"role": "user", "content": prompt})
            try:
                response = await self._complete(messages=synthesis_messages, enable_tools=False)
            except UpstreamServiceError as exc:
                self.logger.warning("Agent synthesis pass failed: %s", exc)
                limitations.append(str(exc))
                return "", limitations

            assistant = response["choices"][0]["message"]
            content = assistant.get("content")
            if content and include_thoughts:
                thoughts.append(content.strip())

            synth_tool_calls = assistant.get("tool_calls") or []
            if synth_tool_calls:
                limitations.append(
                    "The model attempted further tool calls during the final synthesis pass; those were not executed."
                )
                continue

            rendered = self._extract_user_facing_answer(content)
            if rendered:
                return rendered, limitations

            synthesis_messages.append({"role": "assistant", "content": content})

        limitations.append("The model ended the synthesis pass without a user-facing answer.")
        return "", limitations

    def _render_tool_content(self, result: ToolResult) -> str:
        payload = result.llm_content if result.llm_content is not None else result.data
        return json.dumps(payload, ensure_ascii=False)

    def _extract_user_facing_answer(self, content: str | None) -> str:
        if not content:
            return ""
        rendered = THOUGHT_BLOCK_PATTERN.sub("", content).strip()
        return rendered

    def _default_incomplete_answer(self, has_clarification: bool) -> str:
        if has_clarification:
            return "I need one more clarification before I can answer safely."
        return (
            "I retrieved inventory evidence, but I could not complete a grounded final answer from it. "
            "Please retry or narrow the scope so I can finish cleanly."
        )

    def _grounded_fallback_answer(
        self,
        clarification: ClarificationPayload | None,
        inventory_snapshot: dict[str, Any] | None,
    ) -> tuple[str, str, list[str]]:
        if clarification is not None:
            return self._default_incomplete_answer(True), "needs_clarification", []

        if inventory_snapshot:
            coverage = inventory_snapshot.get("coverage") or {}
            limitations = [
                "The model finished retrieval but did not produce a final answer, so the runtime rendered the grounded inventory snapshot directly."
            ]
            status = "limited" if coverage.get("isPartial") or coverage.get("limitations") else "answered"
            return (
                render_inventory_snapshot_markdown(
                    rows=inventory_snapshot.get("rows", []),
                    coverage=coverage,
                ),
                status,
                limitations,
            )

        return self._default_incomplete_answer(False), "limited", []

    def _collect_evidence(self, data: Any, evidence: list[NormalizedEvidence]) -> None:
        if isinstance(data, dict):
            if "provenance" in data and "evidence_paths" in data:
                evidence.append(NormalizedEvidence.model_validate(data))
                return
            for value in data.values():
                self._collect_evidence(value, evidence)
            return

        if isinstance(data, list):
            for item in data:
                self._collect_evidence(item, evidence)

    def _append_unique(self, values: list[str], new_value: str, limit: int = 6) -> list[str]:
        deduped = [item for item in values if item != new_value]
        return ([new_value] + deduped)[:limit]

    def _dedupe(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def _composer_limitations(self, limitations: list[str]) -> list[str]:
        technical_markers = (
            "plan.step",
            "tool",
            "cache",
            "redis",
            "validator",
            "runtime",
            "json",
            "args",
            "status",
            "model output",
            "trace",
        )
        filtered: list[str] = []
        for limitation in self._dedupe(limitations):
            lower = limitation.lower()
            if any(marker in lower for marker in technical_markers):
                continue
            filtered.append(limitation)
        return filtered

    def _build_debug_payload(
        self,
        *,
        request: str,
        plan_status: PlanStatus,
        session_state: SessionState,
        thoughts: list[str],
        traces: list[ToolTrace],
        resolved_items: list[NormalizedEvidence],
        limitations: list[str],
        parallel_batches: list[AgentDebugParallelBatch],
    ) -> AgentDebugPayload:
        return AgentDebugPayload(
            intent=AgentDebugIntent(
                current_goal=plan_status.goal,
                primary_entity_guess=self._primary_entity_guess(resolved_items),
                requested_attributes=[],
                inferred_filters=self._inferred_filters(request, session_state),
                scope_status="prompt_routed",
            ),
            plan=AgentDebugPlan(
                goal=plan_status.goal,
                status=plan_status.status,
                ready_steps=self._ready_step_ids(plan_status),
                blocked_steps=self._blocked_step_ids(plan_status),
                dag=[
                    AgentDebugPlanStep(
                        id=step.id,
                        name=step.name,
                        tool=step.tool,
                        status=step.status,
                        depends_on=list(step.depends_on),
                        parallel_group=step.parallel_group,
                    )
                    for step in plan_status.steps
                ],
                next_hop_rules=self._next_hop_rules(plan_status),
            ),
            retrieval=AgentDebugRetrieval(
                thought_blocks=self._thought_blocks_from_debug(thoughts, traces),
                trace_summary=[
                    AgentDebugTraceSummary(
                        tool=trace.tool,
                        status=trace.status,
                        result_count=trace.result_count,
                        cache_status=trace.cache_status,
                    )
                    for trace in traces
                ],
                parallel_batches=parallel_batches,
            ),
            grounding=AgentDebugGrounding(
                resolved_identifiers=list(session_state.recent_resolved_identifiers[:6]),
                evidence_count=len(resolved_items),
                unresolved_attributes=[],
                user_impact_limitations=self._composer_limitations(limitations),
            ),
        )

    def _primary_entity_guess(self, resolved_items: list[NormalizedEvidence]) -> str:
        if any(item.variant_id or item.variant_name for item in resolved_items):
            return "variant"
        if any(item.product_id or item.product_name for item in resolved_items):
            return "product"
        return "unknown"

    def _inferred_filters(self, request: str, session_state: SessionState) -> dict[str, Any]:
        filters = dict(session_state.last_filters)
        sku_match = re.search(r"\b[a-z0-9]+(?:-[a-z0-9]+){1,}\b", request, re.IGNORECASE)
        if sku_match:
            filters.setdefault("sku", sku_match.group(0))
        elif request.strip():
            filters.setdefault("search", request.strip())
        return filters

    def _ready_step_ids(self, plan_status: PlanStatus) -> list[int]:
        completed = {step.id for step in plan_status.steps if step.status == "done"}
        return [
            step.id
            for step in plan_status.steps
            if step.status != "done" and all(dependency in completed for dependency in step.depends_on)
        ]

    def _blocked_step_ids(self, plan_status: PlanStatus) -> list[int]:
        ready = set(self._ready_step_ids(plan_status))
        return [step.id for step in plan_status.steps if step.status != "done" and step.id not in ready]

    def _next_hop_rules(self, plan_status: PlanStatus) -> list[str]:
        rules = [
            "Use stock.search_catalogue to resolve candidate products before exact detail lookups.",
            "If catalogue search returns identifiers without enough user-facing detail, follow with stock.get_product or stock.extract_variant_evidence.",
            "Do not call stock.extract_variant_evidence with variantId alone; supplement it with sku or id.",
        ]
        pending_recursive = [
            f"Pending recursive follow-up: step {step.id} -> {step.tool}"
            for step in plan_status.steps
            if step.status != "done" and step.name == "recursive detail retrieval"
        ]
        return rules + pending_recursive

    def _thought_blocks_from_debug(self, thoughts: list[str], traces: list[ToolTrace]) -> list[str]:
        blocks: list[str] = []
        seen: set[str] = set()
        sources = list(thoughts) + [trace.thought for trace in traces if trace.thought]
        for source in sources:
            for block in THOUGHT_BLOCK_PATTERN.findall(source):
                compact = block.strip()
                if compact and compact not in seen:
                    seen.add(compact)
                    blocks.append(compact)
        return blocks

    def _utc_now_iso(self) -> str:
        return datetime.now(tz=UTC).isoformat()
