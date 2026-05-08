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
from app.prompt.context import render_plan_context
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
from app.text.utils import lexical_overlap, significant_tokens
from app.tool.registry import ToolRegistry


THOUGHT_BLOCK_PATTERN = re.compile(r"<thought>.*?</thought>", re.IGNORECASE | re.DOTALL)
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_SEARCH_SPLITTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "stock_search",
        "stock_search_catalogue",
        "stock_snapshot",
        "stock_inventory_snapshot",
        "stock_availability",
        "stock_rank",
    }
)


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

    @staticmethod
    def _parse_model_json_content(content: str | None, *, context: str) -> Any:
        # Root Cause vs Logic: planner/formatter sometimes return whitespace-only bodies,
        # or JSON wrapped in ``` fences despite response_format. json.loads("") yields
        # the opaque "Expecting value: line 1 column 1" error and forces fallback plans
        # that over-call tools. We normalize the payload before parsing.
        text = (content or "").strip()
        if not text:
            raise json.JSONDecodeError(f"{context} returned empty message content", text, 0)
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)

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
        replan_rounds_used = 0
        inventory_snapshot_signatures: set[str] = set()

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
                # Root Cause vs Logic: once a stock-only turn already has a
                # complete memo/snapshot path, the autonomous replan pass adds
                # latency without improving coverage and can perturb otherwise
                # stable answers. We skip that extra pass when the completed
                # plan can already be rendered directly from grounded stock rows.
                requested_domains = self._requested_domains(plan_status)
                has_stock_snapshot_answer = self._can_use_stock_snapshot_fast_path(plan_status) and bool(
                    self._memo_stock_rows(session_state.memo_cache)
                )
                if (
                    clarification is None
                    and replan_rounds_used < self.settings.agent_replan_max_rounds
                    and requested_domains != {"stock"}
                    and not has_stock_snapshot_answer
                ):
                    replan_note = await self._append_autonomous_replan_steps(
                        request_message=request.message,
                        plan_status=plan_status,
                        session_state=session_state,
                        traces=traces,
                        limitations=limitations,
                    )
                    if replan_note is not None:
                        replan_rounds_used += 1
                        limitations.append(replan_note)
                        self._persist_plan_state(session_state, plan_status)
                        continue
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

            tool_calls = await self._expand_single_item_search_calls(
                tool_calls=tool_calls,
                request_message=request.message,
            )
            if messages and isinstance(messages[-1], dict):
                messages[-1]["tool_calls"] = tool_calls

            prepared_calls: list[dict[str, Any]] = []
            assistant_thought = (assistant.get("content") or "").strip()
            for tool_call in tool_calls:
                declared_tool = str(tool_call["function"].get("name") or "")
                tool_name = self.tool_registry.resolve_tool_name(declared_tool)
                raw_arguments = tool_call["function"].get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    parsed_args = {"_raw": raw_arguments}
                normalized_args = parsed_args if isinstance(parsed_args, dict) else {"value": parsed_args}
                tool_name, normalized_args, rewrite_note = self._rewrite_variant_family_tool_call(
                    tool_name=tool_name,
                    args=normalized_args,
                )
                if rewrite_note is not None:
                    limitations.append(rewrite_note)

                binding_source = declared_tool if declared_tool != tool_name else None
                plan_step, inserted = self._resolve_or_insert_plan_step(
                    plan_status,
                    tool_name,
                    normalized_args,
                    binding_source_tool=binding_source,
                )
                if plan_step.status != "done":
                    plan_step.status = "in-progress"
                prepared_calls.append(
                    {
                        "tool_call": tool_call,
                        "tool_name": tool_name,
                        "normalized_args": normalized_args,
                        "plan_step": plan_step,
                        "inserted": inserted,
                        "snapshot_signature": self._inventory_snapshot_signature(normalized_args),
                    }
                )

            prepared_calls = self._order_prepared_calls(prepared_calls)
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

            raw_outcomes: list[ToolResult | Exception | dict[str, str]] = []
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
                    if item["plan_step"].status == "done":
                        skip_reason = (
                            item["plan_step"].validation.findings[0]
                            if item["plan_step"].validation and item["plan_step"].validation.findings
                            else (
                                f"Skipped `{item['tool_name']}` because the required evidence was already "
                                "captured earlier in the run."
                            )
                        )
                        raw_outcomes.append(
                            {"skip_reason": skip_reason}
                        )
                        continue
                    snapshot_signature = item.get("snapshot_signature")
                    skip_reason = self._skip_prepared_call_reason(
                        item=item,
                        traces=traces,
                        inventory_snapshot=inventory_snapshot,
                        snapshot_signatures=inventory_snapshot_signatures,
                    )
                    if skip_reason is not None:
                        raw_outcomes.append({"skip_reason": skip_reason})
                        continue
                    try:
                        outcome = await self.tool_registry.call_tool(
                            item["tool_name"],
                            item["normalized_args"],
                            session_id=session_state.session_id,
                            thought=assistant_thought,
                        )
                        raw_outcomes.append(outcome)
                        if outcome.tool in {"stock_snapshot", "stock_inventory_snapshot"} and isinstance(outcome.data, dict):
                            inventory_snapshot = outcome.data
                            prune_notes = self._prune_redundant_stock_steps(
                                session_state=session_state,
                                plan_status=plan_status,
                                completed_step=item["plan_step"],
                                inventory_snapshot=inventory_snapshot,
                            )
                            if prune_notes:
                                limitations.extend(prune_notes)
                                self._persist_plan_state(session_state, plan_status)
                    except Exception as exc:  # pragma: no cover - mirrored error flow
                        raw_outcomes.append(exc)
                    finally:
                        if snapshot_signature:
                            inventory_snapshot_signatures.add(snapshot_signature)

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
                    if isinstance(outcome, dict) and outcome.get("skip_reason"):
                        skip_reason = str(outcome["skip_reason"])
                        if not (
                            plan_step.status == "done"
                            and plan_step.validation is not None
                            and skip_reason in plan_step.validation.findings
                        ):
                            traces.append(
                                ToolTrace(
                                    thought=assistant_thought,
                                    tool=tool_name,
                                    args=normalized_args,
                                    status="skipped",
                                    normalization_notes=[skip_reason],
                                )
                            )
                            limitations.append(skip_reason)
                            validation, _ = self._record_skipped_step(
                                session_state=session_state,
                                plan_status=plan_status,
                                step=plan_step,
                                tool_name=tool_name,
                                tool_args=normalized_args,
                                reason=skip_reason,
                            )
                            plan_step.validation = validation
                            plan_step.status = "done"
                        tool_content = json.dumps({"skipped": {"reason": skip_reason}}, ensure_ascii=False)
                        self._persist_plan_state(session_state, plan_status)
                        tool_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": tool_content,
                            }
                        )
                        continue

                    if isinstance(outcome, Exception):
                        if not isinstance(outcome, supported_tool_errors):
                            raise outcome
                        raise outcome

                    result = outcome
                    if result.tool in {"stock_snapshot", "stock_inventory_snapshot"} and isinstance(result.data, dict):
                        inventory_snapshot = result.data
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
                        # Root Cause vs Logic: complex chat turns can call several
                        # stock tools, and running a separate LLM validator for
                        # every stock result quickly hits Azure 429 limits. Stock
                        # payloads already have deterministic row/evidence
                        # extraction, so reserve the model validator for
                        # non-stock tools that lack a richer local normalizer.
                        use_model_validator=not tool_name.startswith("stock_"),
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
                    prune_notes = self._prune_plan_after_result(
                        session_state=session_state,
                        plan_status=plan_status,
                        completed_step=plan_step,
                        result=result,
                        inventory_snapshot=inventory_snapshot,
                    )
                    if prune_notes:
                        limitations.extend(prune_notes)
                    tool_content = self._render_tool_content(result)
                except supported_tool_errors as exc:
                    error_trace = ToolTrace(
                        thought=assistant_thought,
                        tool=tool_name,
                        args=normalized_args,
                        status="error",
                        error_status_code=exc.status_code if isinstance(exc, UpstreamServiceError) else None,
                        error_request=getattr(exc, "request", None),
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

        stock_snapshot_fast_path = bool(inventory_snapshot and self._can_use_stock_snapshot_fast_path(plan_status))
        memo_stock_rows = self._memo_stock_rows(session_state.memo_cache)
        memo_stock_fast_path = bool(
            not stock_snapshot_fast_path
            and clarification is None
            and self._can_use_stock_snapshot_fast_path(plan_status)
            and memo_stock_rows
        )

        if clarification is None and plan_complete:
            if stock_snapshot_fast_path:
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
            elif memo_stock_fast_path:
                memo_coverage = {
                    "matchedProducts": len({row.get("product") for row in memo_stock_rows if row.get("product")}),
                    "matchedPages": 1,
                    "enrichedProducts": len({row.get("product") for row in memo_stock_rows if row.get("product")}),
                    "enrichedVariants": len(memo_stock_rows),
                    "isPartial": False,
                    "limitations": [],
                }
                draft_answer = render_inventory_snapshot_markdown(
                    rows=memo_stock_rows,
                    coverage=memo_coverage,
                )
                draft_answer = (
                    f"{draft_answer}\n\n"
                    "If you want, I can drill into one specific variant with a deeper breakdown."
                )
                status_hint = "answered"
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
                    allow_snapshot_answer=stock_snapshot_fast_path,
                )
                draft_answer = fallback_answer
                status_hint = fallback_status
                limitations.extend(fallback_limitations)
                used_grounded_snapshot_fallback = bool(stock_snapshot_fast_path and not clarification)

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

        if (
            session_state.current_plan
            and session_state.current_plan.status == "in-progress"
            and self._allow_memory_reuse(session_state)
        ):
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
                raw = self._parse_model_json_content(content, context="Planner")
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
        use_model_validator: bool = True,
    ) -> tuple[PlanValidation, MemoEntry, list[str]]:
        # Motivation vs Logic: every retrieval now writes normalized rows,
        # evidence, and validation findings into a shared memo cache so follow-up
        # steps reuse grounded data instead of re-deriving from raw tool payloads.
        limitations: list[str] = []
        if use_model_validator:
            validator = await self._validator_envelope(
                plan_status=plan_status,
                step=step,
                tool_name=tool_name,
                tool_args=tool_args,
                result=result,
                memo_cache=session_state.memo_cache,
                session_state=session_state,
            )
        else:
            # Root Cause vs Logic: direct `/tools/call` requests were reusing the
            # query-time validator LLM pass, which turned simple deterministic
            # tool invocations into remote model round-trips and made local/mock
            # regression tests hang behind network latency. We keep the shared
            # plan+memo lifecycle but use deterministic normalization here.
            validator = self._fallback_validator_envelope(result)

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

        fallback_rows = self._fallback_rows(result.data)
        fallback_evidence = self._fallback_evidence(result.data)
        # Root Cause vs Logic: the validator samples large stock payloads to fit
        # the token budget, so trusting its partial normalized_rows can drop
        # later chairs from the memo and from the final inventory table. Stock
        # results therefore prefer the full deterministic fallback rows/evidence.
        if tool_name.startswith("stock_") and fallback_rows:
            normalized_rows = fallback_rows
        else:
            normalized_rows = validator.normalized_rows or fallback_rows
        if tool_name.startswith("stock_") and fallback_evidence:
            normalized_evidence = fallback_evidence
        else:
            normalized_evidence = validator.normalized_evidence or fallback_evidence
        subject_provenance = self._subject_provenance(normalized_rows, normalized_evidence, tool_args)

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
                **subject_provenance,
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

    def _record_skipped_step(
        self,
        *,
        session_state: SessionState,
        plan_status: PlanStatus,
        step: PlanStep,
        tool_name: str,
        tool_args: dict[str, Any],
        reason: str,
    ) -> tuple[PlanValidation, MemoEntry]:
        validation = PlanValidation(
            expected_rows=0,
            actual_rows=0,
            cache_status="skipped",
            findings=[reason],
            ambiguity=[],
            missing_statistics=[],
            confidence=1.0,
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
                "skipped": True,
                "reason": reason,
                "captured_at": self._utc_now_iso(),
            },
        )
        session_state.memo_cache.entries.append(memo_entry)
        session_state.memo_cache.aggregates = self._recompute_memo_aggregates(session_state)
        plan_status.memo = session_state.memo_cache
        self._update_plan_metadata(session_state, plan_status, step.id, validation)
        return validation, memo_entry

    def _prune_plan_after_result(
        self,
        *,
        session_state: SessionState,
        plan_status: PlanStatus,
        completed_step: PlanStep,
        result: ToolResult,
        inventory_snapshot: dict[str, Any] | None,
    ) -> list[str]:
        if result.tool not in {"stock_snapshot", "stock_inventory_snapshot"} or inventory_snapshot is None:
            return []
        return self._prune_redundant_stock_steps(
            session_state=session_state,
            plan_status=plan_status,
            completed_step=completed_step,
            inventory_snapshot=inventory_snapshot,
        )

    def _prune_redundant_stock_steps(
        self,
        *,
        session_state: SessionState,
        plan_status: PlanStatus,
        completed_step: PlanStep,
        inventory_snapshot: dict[str, Any],
    ) -> list[str]:
        notes: list[str] = []
        for step in plan_status.steps:
            if step.id == completed_step.id or step.status == "done":
                continue
            reason = self._snapshot_skip_reason(
                tool_name=step.tool,
                args=step.args,
                inventory_snapshot=inventory_snapshot,
            )
            if reason is None:
                continue
            validation, _ = self._record_skipped_step(
                session_state=session_state,
                plan_status=plan_status,
                step=step,
                tool_name=step.tool,
                tool_args=step.args,
                reason=reason,
            )
            step.validation = validation
            step.status = "done"
            notes.append(
                f"Pruned planned step `{step.id}` (`{step.tool}`) because the inventory snapshot already covered that evidence."
            )
        return notes

    async def _validator_envelope(
        self,
        *,
        plan_status: PlanStatus,
        step: PlanStep,
        tool_name: str,
        tool_args: dict[str, Any],
        result: ToolResult,
        memo_cache,
        session_state: SessionState,
    ) -> ValidatorEnvelope:
        if self._client is None:
            return self._fallback_validator_envelope(result)

        try:
            trace_payload = result.trace.model_dump(mode="json") if result.trace else {}
            # Root Cause vs Logic: large inventory snapshots can carry 50+ NormalizedEvidence
            # objects in rows/evidence/items, ballooning the validator prompt to hundreds of
            # kilobytes. The model then has to produce normalized_rows/evidence back within
            # only 1400 completion tokens — impossible — so it returns null content.
            # We sample a small slice here; quality checks (findings, ambiguity, confidence)
            # are still meaningful on a representative subset, and the full rows/evidence are
            # reconstructed deterministically from result.data by the fallback path.
            tool_payload = self._sample_tool_payload_for_validator(
                result.llm_content if result.llm_content is not None else result.data
            )

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
                                active_subject=session_state.active_subject,
                                memory_scope=session_state.memory_scope,
                            ),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                    "max_completion_tokens": 1400,
                }
                response_payload = await self._post_chat_completion(payload, endpoint_name="/api/v1/query/validator")
                content = response_payload["choices"][0]["message"].get("content") or ""
                raw = self._parse_model_json_content(content, context="Validator")
                return ValidatorEnvelope.model_validate(raw)

            return await self._retry_on_context_limit("validator", run_mode)
        except (UpstreamServiceError, json.JSONDecodeError, ValidationError) as exc:
            self.logger.warning("Validator pass failed, using deterministic fallback: %s", exc)
            fallback = self._fallback_validator_envelope(result)
            fallback.findings.append("Validator model output was invalid; deterministic fallback normalization was used.")
            return fallback

    def _sample_tool_payload_for_validator(self, payload: Any, max_items: int = 6) -> Any:
        # Motivation vs Logic: the validator only needs a representative sample to
        # produce findings, ambiguity flags, and a confidence score. Re-emitting
        # the full rows/evidence in the 1400-token completion budget is impossible
        # for large snapshots, so we cap the heaviest list fields here. The full
        # data is always reconstructed from result.data by the fallback path.
        if not isinstance(payload, dict):
            return payload
        sampled = dict(payload)
        for key in ("rows", "evidence", "items"):
            value = sampled.get(key)
            if isinstance(value, list) and len(value) > max_items:
                sampled[key] = value[:max_items]
        return sampled

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
            variant_rows = self._fallback_variant_rows_from_items(data.get("items"))
            if variant_rows:
                return variant_rows
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
        variant_evidence = self._fallback_variant_evidence_from_items(data)
        if variant_evidence:
            return variant_evidence
        evidence: list[NormalizedEvidence] = []
        self._collect_evidence(data, evidence)
        return [item.model_dump(mode="json") for item in evidence]

    def _fallback_variant_rows_from_items(self, items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            product_name = (item.get("name") or "").strip() or None
            product_api_id = item.get("id")
            product_id_str = str(product_api_id).strip() if product_api_id is not None else None
            if product_id_str == "":
                product_id_str = None
            variants = item.get("variants")
            if not isinstance(variants, list):
                continue
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                details = variant.get("details")
                if not isinstance(details, dict):
                    details = {}
                variant_api_id = variant.get("id")
                variant_id_str = str(variant_api_id).strip() if variant_api_id is not None else None
                if variant_id_str == "":
                    variant_id_str = None
                # Motivation vs Logic: per-variant memo rows are used to recover
                # `stock_detail` / planner follow-ups. Rows previously exposed
                # only names and optional SKU, so `id` was missing after search and
                # the runtime could not run product detail steps without a manual SKU.
                rows.append(
                    {
                        "product": product_name,
                        "product_id": product_id_str,
                        "variant": (variant.get("name") or "").strip() or None,
                        "variant_id": variant_id_str,
                        "sku": self._compact_identifier(variant.get("sku")),
                        "size": self._format_dimensions_from_details(details),
                        "stock": self._format_regional_stock_from_details(details, variant.get("totalHirable")),
                    }
                )
        return [row for row in rows if any(value is not None for value in row.values())]

    def _fallback_variant_evidence_from_items(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        evidence_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            product_name = (item.get("name") or "").strip() or None
            variants = item.get("variants")
            if not isinstance(variants, list):
                continue
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                details = variant.get("details")
                if not isinstance(details, dict):
                    details = {}
                p_id = item.get("id")
                v_id = variant.get("id")
                evidence_items.append(
                    {
                        "product_name": product_name,
                        "product_id": str(p_id).strip() if p_id is not None and str(p_id).strip() else None,
                        "variant_name": (variant.get("name") or "").strip() or None,
                        "variant_id": str(v_id).strip() if v_id is not None and str(v_id).strip() else None,
                        "sku": self._compact_identifier(variant.get("sku")),
                        "dimensions": {
                            "length": details.get("length"),
                            "width": details.get("width"),
                            "height": details.get("height"),
                        },
                        "stock": {
                            "vicStock": details.get("vicStock"),
                            "vicHirable": details.get("vicHirable"),
                            "nswStock": details.get("nswStock"),
                            "nswHirable": details.get("nswHirable"),
                            "qldStock": details.get("qldStock"),
                            "qldHirable": details.get("qldHirable"),
                            "totalStock": details.get("totalStock"),
                            "totalHirable": variant.get("totalHirable"),
                        },
                    }
                )
        return evidence_items

    def _format_dimensions_from_details(self, details: dict[str, Any]) -> str | None:
        parts = [details.get("length"), details.get("width"), details.get("height")]
        values = [value for value in parts if isinstance(value, (int, float))]
        if not values:
            return None
        rendered = " x ".join(f"{float(value):g}" for value in values)
        return f"{rendered} m"

    def _format_regional_stock_from_details(self, details: dict[str, Any], total_hirable: Any) -> str | None:
        segments: list[str] = []
        total_stock = details.get("totalStock")
        if isinstance(total_stock, int):
            if isinstance(total_hirable, int):
                segments.append(f"Overall has {total_stock} in stock, with {total_hirable} available for hire")
            else:
                segments.append(f"Overall has {total_stock} in stock")
        location_descriptions: list[str] = []
        for location, stock_key, hirable_key in (
            ("VIC", "vicStock", "vicHirable"),
            ("NSW", "nswStock", "nswHirable"),
            ("QLD", "qldStock", "qldHirable"),
        ):
            stock_value = details.get(stock_key)
            hirable_value = details.get(hirable_key)
            if not isinstance(stock_value, int) and not isinstance(hirable_value, int):
                continue
            if isinstance(stock_value, int) and isinstance(hirable_value, int):
                location_descriptions.append(f"{location}: {stock_value} stock, {hirable_value} hirable")
            elif isinstance(stock_value, int):
                location_descriptions.append(f"{location}: {stock_value} stock")
            elif isinstance(hirable_value, int):
                location_descriptions.append(f"{location}: {hirable_value} hirable")
        if location_descriptions:
            segments.append("By location: " + "; ".join(location_descriptions))
        if not segments:
            return None
        return ". ".join(segments) + "."

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

    def _order_prepared_calls(self, prepared_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(prepared_calls) < 2:
            return prepared_calls
        # Motivation vs Logic: answer-ready snapshot retrieval can satisfy broad
        # family queries on its own, so we run it before narrower stock-detail
        # tools and give the runtime a chance to prune redundant follow-up work.
        return sorted(
            prepared_calls,
            key=lambda item: (
                0 if item["tool_name"] in {"stock_snapshot", "stock_inventory_snapshot"} else 1,
                item["plan_step"].id,
            ),
        )

    def _should_execute_parallel(self, prepared_calls: list[dict[str, Any]]) -> bool:
        if len(prepared_calls) < 2:
            return False
        tool_names = {str(item["tool_name"]) for item in prepared_calls}
        if tool_names.intersection({"stock_snapshot", "stock_inventory_snapshot"}) and tool_names.intersection(
            {
                "stock_compare",
                "stock_compare_variants",
                "stock_extract_variant_evidence",
                "stock_get_variant_evidence",
                "stock_detail",
                "stock_get_product",
            }
        ):
            return False
        return not any(str(item["tool_name"]).startswith("session_") for item in prepared_calls)

    def _skip_prepared_call_reason(
        self,
        *,
        item: dict[str, Any],
        traces: list[ToolTrace],
        inventory_snapshot: dict[str, Any] | None,
        snapshot_signatures: set[str] | None = None,
    ) -> str | None:
        tool_name = str(item["tool_name"])
        normalized_args = item["normalized_args"]
        if tool_name in {"stock_search", "stock_search_catalogue"}:
            search_signature = self._catalogue_search_signature(normalized_args)
            if search_signature is not None:
                for trace in traces:
                    if trace.tool != tool_name:
                        continue
                    prior_signature = self._catalogue_search_signature(trace.args)
                    if prior_signature != search_signature:
                        continue
                    if trace.status == "ok":
                        return (
                            "Skipped `stock_search` because this semantic search was already "
                            "resolved earlier in this run."
                        )
                    if trace.status == "error":
                        return (
                            "Skipped `stock_search` because an equivalent semantic search already "
                            "failed earlier in this run."
                        )

        # Root Cause vs Logic: identical successful tool calls were allowed to
        # run repeatedly in a single query turn, which created long retrieval
        # churn on follow-up prompts. Skip exact-success repeats to keep the
        # loop progressing toward composition.
        for trace in traces:
            if trace.status != "ok" or trace.tool != tool_name or trace.args != normalized_args:
                continue
            return (
                f"Skipped `{tool_name}` because the same arguments already succeeded earlier in this run; "
                "the runtime will not repeat an identical retrieval pattern."
            )
        for trace in traces:
            if trace.status != "error" or trace.tool != tool_name or trace.args != normalized_args:
                continue
            return (
                f"Skipped `{tool_name}` because the same arguments already failed earlier in this run; "
                "the runtime will not repeat an identical failing retrieval pattern."
            )

        if inventory_snapshot is not None:
            snapshot_reason = self._snapshot_skip_reason(
                tool_name=tool_name,
                args=normalized_args,
                inventory_snapshot=inventory_snapshot,
            )
            if snapshot_reason is not None:
                return snapshot_reason
        snapshot_signature = item.get("snapshot_signature")
        if (
            snapshot_signature
            and snapshot_signatures
            and snapshot_signature in snapshot_signatures
            and item.get("inserted")
        ):
            return self._snapshot_duplicate_reason(snapshot_signature)
        return None

    def _snapshot_skip_reason(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        inventory_snapshot: dict[str, Any],
    ) -> str | None:
        coverage = inventory_snapshot.get("coverage") or {}
        if coverage.get("isPartial"):
            return None

        snapshot_identifiers = self._snapshot_identifier_set(inventory_snapshot)
        if not snapshot_identifiers:
            return None

        if tool_name in {"stock_compare", "stock_compare_variants"}:
            identifiers = args.get("identifiers")
            if isinstance(identifiers, list) and identifiers and all(
                self._compact_identifier(identifier) in snapshot_identifiers for identifier in identifiers
            ):
                return (
                    "Skipped `stock_compare` because the inventory snapshot already contains "
                    "the requested variant evidence for this family."
                )

        if tool_name in {"stock_extract_variant_evidence", "stock_get_variant_evidence", "stock_detail", "stock_get_product"}:
            requested_identifiers = [
                self._compact_identifier(args.get("sku")),
                self._compact_identifier(args.get("id")),
                self._compact_identifier(args.get("variantId")),
            ]
            requested_identifiers = [identifier for identifier in requested_identifiers if identifier]
            if requested_identifiers and all(identifier in snapshot_identifiers for identifier in requested_identifiers):
                return (
                    f"Skipped `{tool_name}` because the inventory snapshot already resolved the "
                    "requested stock evidence."
                )
        return None

    def _snapshot_identifier_set(self, inventory_snapshot: dict[str, Any]) -> set[str]:
        identifiers: set[str] = set()
        evidence = inventory_snapshot.get("evidence")
        if not isinstance(evidence, list):
            return identifiers
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for key in ("product_id", "variant_id", "sku"):
                identifier = self._compact_identifier(item.get(key))
                if identifier:
                    identifiers.add(identifier)
        return identifiers

    def _inventory_snapshot_signature(self, args: dict[str, Any]) -> str | None:
        category = args.get("categoryId")
        search = (args.get("search") or "").strip().lower()
        if not category and not search:
            return None
        page = self._normalize_positive_int(args.get("page"), 1)
        page_size = self._normalize_positive_int(args.get("pageSize"), 20)
        parts: list[str] = []
        if category:
            parts.append(f"category:{category}")
        if search:
            parts.append(f"search:{search}")
        parts.append(f"page:{page}")
        parts.append(f"pageSize:{page_size}")
        return "|".join(parts)

    def _snapshot_duplicate_reason(self, signature: str) -> str:
        label = self._snapshot_signature_label(signature)
        return (
            f"Skipped `stock_snapshot` because the query already covered {label}."
        )

    @staticmethod
    def _snapshot_signature_label(signature: str) -> str:
        parts = []
        for segment in signature.split("|"):
            if ":" not in segment:
                continue
            key, value = segment.split(":", 1)
            if key in {"category", "search"}:
                parts.append(f"{key}={value}")
        if parts:
            return ", ".join(parts)
        return signature

    @staticmethod
    def _normalize_positive_int(value: Any, default: int) -> int:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return default
        if numeric <= 0:
            return default
        return numeric

    def _append_recursive_follow_up(
        self,
        *,
        request_message: str,
        plan_status: PlanStatus,
        completed_step: PlanStep,
        result: ToolResult,
    ) -> str | None:
        if result.tool not in {"stock_search", "stock_search_catalogue"}:
            return None
        if completed_step.name == "recursive detail retrieval":
            return None

        # Root Cause vs Logic: the old global "any pending detail step" guard
        # stopped later catalogue hits from scheduling their own detail fetches,
        # so multi-item searches could only enrich the first matched product.
        # We now dedupe exact lookup args inside the fan-out loop and allow
        # distinct products to keep adding follow-up steps.

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
    ) -> list[tuple[str, dict[str, Any]]]:
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return []

        # Motivation vs Logic: single-winner follow-up selection caused
        # multi-item asks (e.g. "Alto and Spencer") to drop valid families.
        # We now branch across ranked catalogue items and cap fan-out via
        # settings so retrieval stays comprehensive but bounded.
        page_size = data.get("pageSize")
        normalized_page_size = page_size if isinstance(page_size, int) and page_size > 0 else self.settings.agent_get_product_page_size
        ranked_items = self._rank_follow_up_items(items, request_message)
        max_products = max(1, self.settings.agent_recursive_follow_up_max_products)

        follow_up_steps: list[tuple[str, dict[str, Any]]] = []
        seen_identifiers: set[str] = set()
        for item in ranked_items:
            lookup_args = self._build_product_follow_up_args(item, normalized_page_size)
            if lookup_args is None:
                continue
            identifier = self._compact_identifier(lookup_args.get("sku")) or self._compact_identifier(lookup_args.get("id"))
            if not identifier or identifier in seen_identifiers:
                continue
            seen_identifiers.add(identifier)
            follow_up_steps.append(("stock_detail", lookup_args))
            if len(follow_up_steps) >= max_products:
                break
        return follow_up_steps

    def _rank_follow_up_items(self, items: list[Any], request_message: str | None) -> list[dict[str, Any]]:
        dict_items = [item for item in items if isinstance(item, dict)]
        if not request_message:
            return dict_items
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in dict_items:
            parts = [str(item.get("name") or "")]
            variants = item.get("variants")
            if isinstance(variants, list):
                parts.extend(str(variant.get("name") or "") for variant in variants if isinstance(variant, dict))
            candidate_text = " ".join(part for part in parts if part)
            scored.append((lexical_overlap(request_message, candidate_text), item))
        scored.sort(key=lambda entry: entry[0], reverse=True)
        return [item for _, item in scored]

    def _build_product_follow_up_args(self, item: dict[str, Any], page_size: int) -> dict[str, Any] | None:
        variants = item.get("variants")
        preferred_sku = None
        if isinstance(variants, list):
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                preferred_sku = self._compact_identifier(variant.get("sku"))
                if preferred_sku:
                    break
        if preferred_sku:
            return {"sku": preferred_sku, "page": 1, "pageSize": page_size}
        product_id = self._compact_identifier(item.get("id"))
        if product_id:
            return {"id": product_id, "page": 1, "pageSize": page_size}
        return None

    def _rewrite_variant_family_tool_call(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        if tool_name not in {"stock_extract_variant_evidence", "stock_get_variant_evidence"}:
            return tool_name, args, None
        compact_id = self._compact_identifier(args.get("id"))
        compact_sku = self._compact_identifier(args.get("sku"))
        compact_variant_id = self._compact_identifier(args.get("variantId"))
        if not compact_id or compact_sku or compact_variant_id:
            return tool_name, args, None

        # Root Cause vs Logic: family-level queries were routed through the
        # variant tool with product id only, which triggers multi-variant
        # safety failures. Rewrite those calls to capped product-detail
        # retrieval instead of unbounded per-variant SKU hydration.
        rewritten_args = {
            "id": compact_id,
            "page": 1,
            "pageSize": self.settings.agent_get_product_page_size,
        }
        return (
            "stock_detail",
            rewritten_args,
            (
                "Rewrote a variant-evidence call with product-id-only args to `stock_detail` "
                "so family-level variant coverage can continue without unsafe SKU guessing."
            ),
        )

    async def _expand_single_item_search_calls(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        request_message: str,
    ) -> list[dict[str, Any]]:
        expanded_calls: list[dict[str, Any]] = []
        seen_signatures: set[tuple[str, str]] = set()

        def append_unique(call: dict[str, Any]) -> None:
            function_payload = call.get("function")
            if not isinstance(function_payload, dict):
                expanded_calls.append(call)
                return
            signature = (
                str(function_payload.get("name") or ""),
                str(function_payload.get("arguments") or ""),
            )
            if signature in seen_signatures:
                return
            seen_signatures.add(signature)
            expanded_calls.append(call)

        # Root Cause vs Logic: planner outputs could merge multiple product
        # names into one search phrase, which under-retrieved catalogue
        # families. Expand and dedupe into one search call per inferred item.
        for tool_call in tool_calls:
            function = tool_call.get("function")
            if not isinstance(function, dict):
                append_unique(tool_call)
                continue
            function_name = str(function.get("name") or "")
            try:
                resolved_tool_name = self.tool_registry.resolve_tool_name(function_name)
            except Exception:
                append_unique(tool_call)
                continue
            if resolved_tool_name not in _SEARCH_SPLITTABLE_TOOLS:
                append_unique(tool_call)
                continue

            raw_arguments = function.get("arguments") or "{}"
            try:
                parsed_args = json.loads(raw_arguments)
            except json.JSONDecodeError:
                append_unique(tool_call)
                continue
            if not isinstance(parsed_args, dict):
                append_unique(tool_call)
                continue

            search_term = parsed_args.get("search")
            if not isinstance(search_term, str) or not search_term.strip():
                append_unique(tool_call)
                continue

            split_terms = await self._split_search_terms_for_single_item_search(
                request_message=request_message,
                search_term=search_term,
            )
            if len(split_terms) <= 1:
                append_unique(tool_call)
                continue

            base_id = str(tool_call.get("id") or "tool_call")
            for index, split_term in enumerate(split_terms, start=1):
                split_args = dict(parsed_args)
                split_args["search"] = split_term
                append_unique(
                    {
                        **tool_call,
                        "id": f"{base_id}_split_{index}",
                        "function": {
                            **function,
                            "arguments": json.dumps(split_args, ensure_ascii=False),
                        },
                    }
                )
        return expanded_calls

    async def _split_search_terms_for_single_item_search(
        self,
        *,
        request_message: str,
        search_term: str,
    ) -> list[str]:
        heuristic_terms = self._heuristic_multi_item_search_terms(search_term)
        if len(heuristic_terms) > 1:
            return heuristic_terms[: self.settings.agent_search_split_max_items]

        if self._client is None:
            return [search_term]
        payload = {
            "model": self.settings.foundry_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Split a stock catalogue search phrase into one-item-at-a-time product search terms. "
                        "Return strict JSON with key `items` as an ordered array of search strings. "
                        "Keep each term self-contained for first-pass search. "
                        "If a single product phrase contains a distinctive model/proper-name token plus generic "
                        "descriptors, include the original phrase first and then a shorter fallback term made only "
                        "from the distinctive product/model token(s). "
                        "If the input already targets one item and has no useful shorter fallback, return it unchanged."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": request_message,
                            "search": search_term,
                            "max_items": self.settings.agent_search_split_max_items,
                            "schema": {"items": ["string"]},
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 220,
        }
        try:
            response_payload = await self._post_chat_completion(payload, endpoint_name="/api/v1/query/search-split")
            content = response_payload["choices"][0]["message"].get("content") or ""
            try:
                raw = self._parse_model_json_content(content, context="Search split")
            except json.JSONDecodeError:
                return [search_term]
        except (UpstreamServiceError, json.JSONDecodeError):
            return [search_term]
        if not isinstance(raw, dict):
            return [search_term]
        values = raw.get("items")
        if not isinstance(values, list):
            return [search_term]

        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = " ".join(str(value).split()).strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            cleaned.append(normalized)
            if len(cleaned) >= self.settings.agent_search_split_max_items:
                break
        return cleaned or [search_term]

    def _heuristic_multi_item_search_terms(self, search_term: str) -> list[str]:
        normalized_search = " ".join(str(search_term or "").split()).strip()
        if not normalized_search:
            return [search_term]

        candidate = re.sub(r"\s*,\s*(?:and|&)\s+", ", ", normalized_search, flags=re.IGNORECASE)
        candidate = re.sub(r"\s+(?:and|&)\s+", ", ", candidate, flags=re.IGNORECASE)
        parts = [part.strip(" ,") for part in candidate.split(",") if part.strip(" ,")]
        if len(parts) <= 1:
            return [search_term]

        # Root Cause vs Logic: list-style requests such as "Baxter, Charlie, and
        # Alto chair" were routed into one combined stock search, so upstream
        # search could fail every family at once. Build one term per product and
        # preserve the shared suffix from the final item before any LLM fallback.
        last_tokens = self._case_preserving_tokens(parts[-1])
        shared_suffix = last_tokens[1:] if len(last_tokens) > 1 else []

        expanded: list[str] = []
        seen: set[str] = set()
        for index, part in enumerate(parts):
            tokens = self._case_preserving_tokens(part)
            normalized_part = " ".join(part.split())
            if index < len(parts) - 1 and shared_suffix and len(tokens) == 1:
                normalized_part = " ".join([tokens[0], *shared_suffix])
            self._append_unique_search_term(expanded, seen, normalized_part)
            if index < len(parts) - 1:
                self._append_shorter_search_fallback(expanded, seen, normalized_part)
        self._append_shorter_search_fallback(expanded, seen, parts[-1])
        return expanded or [search_term]

    def _append_shorter_search_fallback(self, values: list[str], seen: set[str], term: str) -> None:
        tokens = self._case_preserving_tokens(term)
        if len(tokens) < 2:
            return
        fallback = " ".join(tokens[:-1]).strip()
        if fallback:
            self._append_unique_search_term(values, seen, fallback)

    def _append_unique_search_term(self, values: list[str], seen: set[str], term: str) -> None:
        normalized = " ".join(str(term or "").split()).strip()
        if not normalized:
            return
        lowered = normalized.casefold()
        if lowered in seen:
            return
        seen.add(lowered)
        values.append(normalized)

    def _case_preserving_tokens(self, value: str) -> list[str]:
        return [token for token in re.findall(r"[A-Za-z0-9]+", value or "") if token]

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
                steps.append(("stock_extract_variant_evidence", lookup))
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
        if result.tool not in {"stock_disambiguate", "resolver_disambiguate_candidates"}:
            return None
        if not isinstance(result.data, dict):
            return None
        if result.data.get("status") != "resolved_product_family":
            return None

        product_id = self._compact_identifier(result.data.get("product_id"))
        if not product_id:
            return None
        follow_up_tool = "stock_detail"
        follow_up_args: dict[str, Any] = {
            "id": product_id,
            "page": 1,
            "pageSize": self.settings.agent_get_product_page_size,
        }
        if any(
            step.tool == follow_up_tool and step.args == follow_up_args
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
                    "Resolver narrowed ambiguity to one product family, so retrieve capped product details."
                ],
                validation=None,
            )
        )
        return (
            f"Resolved product family follow-up step `{next_id}` was added for `{follow_up_tool}` "
            "so the final answer includes capped variants before any clarification prompt."
        )


    def _pending_variant_evidence_step_for_get_product_rewrite(
        self,
        plan_status: PlanStatus,
        get_product_args: dict[str, Any],
    ) -> PlanStep | None:
        # Root Cause vs Logic: `_rewrite_variant_family_tool_call` turns
        # product-id-only `stock_extract_variant_evidence` into `stock_detail`.
        # Without binding, we matched/inserted a get_product step while the
        # original EVE plan row stayed not-done, bloating the DAG and often
        # burning `agent_max_steps` on no-op "next step 2" cycles.
        compact_id = self._compact_identifier(get_product_args.get("id"))
        if not compact_id:
            return None
        for step in plan_status.steps:
            if step.status == "done":
                continue
            if step.tool not in {"stock_extract_variant_evidence", "stock_get_variant_evidence"}:
                continue
            sid = self._compact_identifier((step.args or {}).get("id"))
            if sid and sid == compact_id:
                return step
        return None

    def _resolve_or_insert_plan_step(
        self,
        plan_status: PlanStatus,
        tool_name: str,
        args: dict[str, Any],
        *,
        binding_source_tool: str | None = None,
    ) -> tuple[PlanStep, bool]:
        # Root Cause vs Logic: bind rewritten family detail retrieval back to
        # the planner's variant-evidence row so that step can complete.
        if (
            tool_name in {"stock_detail", "stock_get_product"}
            and binding_source_tool in {"stock_extract_variant_evidence", "stock_get_variant_evidence"}
        ):
            bound = self._pending_variant_evidence_step_for_get_product_rewrite(plan_status, args)
            if bound is not None:
                merged = {**(bound.args or {}), **args}
                bound.args = merged
                return bound, False

        # Root Cause vs Logic: binding by tool name alone merged distinct
        # multi-item calls into one pending step and overwrote earlier args.
        # We now bind by exact args first, then only reuse empty placeholders.
        if tool_name in {"stock_search", "stock_search_catalogue"}:
            incoming_signature = self._catalogue_search_signature(args)
            if incoming_signature is not None:
                for step in plan_status.steps:
                    if step.tool != tool_name:
                        continue
                    if self._catalogue_search_signature(step.args) != incoming_signature:
                        continue
                    return step, False

        # Root Cause vs Logic: completed steps with identical args were not
        # reused, so runtime could reinsert the same retrieval as a new step
        # and loop. Reuse done steps first to keep plan execution monotonic.
        for step in plan_status.steps:
            if step.tool == tool_name and step.status == "done" and step.args == args:
                return step, False
        for step in plan_status.steps:
            if step.tool == tool_name and step.status != "done" and step.args == args:
                return step, False
        for step in plan_status.steps:
            if step.tool == tool_name and step.status != "done" and not step.args and args:
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
        # Root Cause vs Logic: capability-only plans intentionally have no tool
        # steps; treat planner-complete empty plans as complete so the composer,
        # not a hard-coded shortcut, can answer from prompt-provided policy data.
        if not plan_status.steps:
            return plan_status.status == "complete"
        return all(step.status == "done" for step in plan_status.steps)

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
        if step.tool in {"stock_extract_variant_evidence", "stock_get_variant_evidence"}:
            return "Please share the exact variant SKU (or product ID plus variant ID) so I can continue safely."
        return "Please share the exact product SKU or product ID so I can continue safely."

    def _resolve_planned_step_args(
        self,
        step: PlanStep,
        session_state: SessionState,
    ) -> tuple[dict[str, Any] | None, str | None]:
        args = dict(step.args or {})
        if step.tool in {"stock_detail", "stock_get_product"}:
            return self._resolve_product_lookup_args(step_id=step.id, args=args, session_state=session_state)
        if step.tool in {"stock_extract_variant_evidence", "stock_get_variant_evidence"}:
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
                    f"Planned step `{step_id}` could not run because `stock_detail` "
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
                        "`stock_extract_variant_evidence` also needs `sku` or `id` to resolve product details."
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
                    "`stock_extract_variant_evidence` requires `id`, `sku`, or `variantId`, "
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
        if not self._allow_memory_reuse(session_state):
            return None
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
        if not self._allow_memory_reuse(session_state):
            return None
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
        for key, target in [
            ("sku", "sku"),
            ("product_id", "id"),
            ("productId", "id"),
            ("id", "id"),
            ("variant_id", "id"),
            ("variantId", "id"),
        ]:
            candidate = self._compact_identifier(entry.args.get(key))
            if candidate:
                return {target: candidate}

        for collection in [entry.evidence, entry.rows]:
            for item in collection:
                if not isinstance(item, dict):
                    continue
                for key, target in [
                    ("sku", "sku"),
                    ("product_id", "id"),
                    ("productId", "id"),
                    ("id", "id"),
                    ("variant_id", "id"),
                    ("variantId", "id"),
                ]:
                    candidate = self._compact_identifier(item.get(key))
                    if candidate:
                        return {target: candidate}
        return None

    def _subject_provenance(
        self,
        normalized_rows: list[dict[str, Any]],
        normalized_evidence: list[dict[str, Any]],
        tool_args: dict[str, Any],
    ) -> dict[str, Any]:
        identifiers: list[str] = []
        names: list[str] = []
        for source in [tool_args, *normalized_rows, *normalized_evidence]:
            if not isinstance(source, dict):
                continue
            for key in ("id", "product_id", "productId", "variant_id", "variantId", "sku"):
                value = self._compact_identifier(source.get(key))
                if value and value not in identifiers:
                    identifiers.append(value)
            for key in ("product", "variant", "product_name", "variant_name", "name", "label"):
                value = source.get(key)
                if isinstance(value, str):
                    compact = value.strip()
                    if compact and compact not in names:
                        names.append(compact)
        return {
            "subject_identifiers": identifiers[:8],
            "subject_names": names[:8],
        }

    def _compact_identifier(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        compact = value.strip()
        return compact or None

    def _catalogue_search_signature(self, args: Any) -> tuple[str, int | None, str | None] | None:
        if not isinstance(args, dict):
            return None
        raw_search = args.get("search")
        if not isinstance(raw_search, str):
            return None
        tokens = sorted(token for token in re.split(r"[^a-z0-9]+", raw_search.lower()) if token)
        if not tokens:
            return None
        department_id = args.get("departmentId")
        normalized_department = department_id if isinstance(department_id, int) else None
        category_id = args.get("categoryId")
        normalized_category = str(category_id).strip().lower() if isinstance(category_id, str) and category_id.strip() else None
        return (" ".join(tokens), normalized_department, normalized_category)

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

        intent_classes = [str(value).strip().lower() for value in parsed.intent_classes if str(value).strip()]
        if not sanitized_steps and "capability" in intent_classes:
            return PlanStatus(
                goal=parsed.goal or request,
                intent_classes=intent_classes,
                steps=[],
                memo=session_state.memo_cache.model_copy(deep=True),
                status="complete",
            )

        if not sanitized_steps:
            return self._fallback_plan(request, session_state)

        memo = session_state.memo_cache.model_copy(deep=True)
        if parsed.memo.entries:
            memo.entries.extend(parsed.memo.entries)
            memo.aggregates = parsed.memo.aggregates or memo.aggregates

        plan = PlanStatus(
            goal=parsed.goal or request,
            intent_classes=intent_classes,
            steps=sanitized_steps,
            memo=memo,
            status="in-progress",
        )
        return plan

    def _fallback_plan(self, request: str, session_state: SessionState) -> PlanStatus:
        tool_names = [tool.name for tool in self.tool_registry.list_tools()]
        default_tool = tool_names[0] if tool_names else "session_state"
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
                                active_subject=session_state.active_subject,
                                memory_scope=session_state.memory_scope,
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
            if not self._should_retry_with_compact_context(exc):
                raise
            # Root Cause vs Logic: upstream prompt rejections can come from
            # either oversized context or policy-sensitive prompt assembly.
            # Retrying with the compact prompt trims non-essential context and
            # gives us a second chance before surfacing a hard backend failure.
            self.logger.warning("Prompt rejected during %s; retrying with compact prompt.", operation_name)
            return await builder("compact")

    def _is_context_length_error(self, exc: UpstreamServiceError) -> bool:
        detail = exc.detail.lower()
        return "context_length_exceeded" in detail or "input tokens exceed" in detail or "messages resulted in" in detail

    def _is_invalid_prompt_error(self, exc: UpstreamServiceError) -> bool:
        detail = exc.detail.lower()
        return (
            "invalid_prompt" in detail
            or "flagged as potentially violating" in detail
            or "usage policy" in detail
        )

    def _should_retry_with_compact_context(self, exc: UpstreamServiceError) -> bool:
        return self._is_context_length_error(exc) or self._is_invalid_prompt_error(exc)

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
            content = response_payload["choices"][0]["message"].get("content")
            raw = self._parse_model_json_content(content, context="Formatter")
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
        response: httpx.Response | None = None
        timeout_attempt = 1
        rate_limit_attempt = 1
        while True:
            try:
                response = await self._client.post("/chat/completions", json=payload)
                if response.status_code == 429:
                    delay = self._foundry_response_retry_delay_seconds(response, rate_limit_attempt)
                    # Root Cause vs Logic: Azure 429s are quota pacing, not a bad
                    # user request. Chat UX should wait through rate pressure and
                    # resume automatically instead of returning a failed answer.
                    self.logger.warning(
                        "Azure AI Foundry rate limited `%s`; retrying until success (attempt %s) in %.1fs.",
                        endpoint_name,
                        rate_limit_attempt,
                        delay,
                    )
                    await anyio.sleep(delay)
                    rate_limit_attempt += 1
                    continue
                break
            except httpx.ReadTimeout as exc:
                if timeout_attempt >= self.settings.foundry_max_attempts:
                    raise UpstreamServiceError(
                        504,
                        f"Azure AI Foundry timed out while handling `{endpoint_name}`.",
                    ) from exc
                delay = self._foundry_retry_delay_seconds(timeout_attempt)
                # Root Cause vs Logic: we expose the retry so operators know we
                # are waiting through transient network hiccups instead of failing.
                self.logger.warning(
                    "Azure AI Foundry timed out while handling `%s`; retry %s/%s in %.1fs.",
                    endpoint_name,
                    timeout_attempt,
                    self.settings.foundry_max_attempts,
                    delay,
                )
                await anyio.sleep(delay)
                timeout_attempt += 1
            except httpx.HTTPError as exc:
                raise UpstreamServiceError(
                    502,
                    f"Azure AI Foundry request failed while handling `{endpoint_name}`: {exc}",
                ) from exc
        if response is None:
            raise UpstreamServiceError(
                504,
                f"Azure AI Foundry timed out while handling `{endpoint_name}`.",
            )

        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamServiceError(
                502,
                f"Azure AI Foundry returned non-JSON while handling `{endpoint_name}`.",
            ) from exc

    def _foundry_retry_delay_seconds(self, attempt: int) -> float:
        delay = self.settings.foundry_retry_backoff_seconds * (2 ** (attempt - 1))
        return min(delay, self.settings.foundry_retry_backoff_cap_seconds)

    def _foundry_response_retry_delay_seconds(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), self.settings.foundry_retry_backoff_cap_seconds)
            except ValueError:
                pass
        return self._foundry_retry_delay_seconds(attempt)

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

    def _can_use_stock_snapshot_fast_path(self, plan_status: PlanStatus) -> bool:
        requested_domains = self._requested_domains(plan_status)
        return requested_domains == {"stock"}

    def _memo_stock_rows(self, memo_cache: Any) -> list[dict[str, Any]]:
        entries = getattr(memo_cache, "entries", None)
        if not isinstance(entries, list):
            return []
        rows: list[dict[str, Any]] = []
        for entry in entries:
            tool_name = getattr(entry, "tool", "")
            if tool_name not in {"stock_detail", "stock_get_product", "stock_snapshot", "stock_inventory_snapshot"}:
                continue
            candidate_rows = getattr(entry, "rows", [])
            if not isinstance(candidate_rows, list):
                continue
            for row in candidate_rows:
                if not isinstance(row, dict):
                    continue
                if not row.get("product") or not row.get("variant"):
                    continue
                if not row.get("size") and not row.get("stock"):
                    continue
                rows.append(
                    {
                        "product": row.get("product"),
                        "variant": row.get("variant"),
                        "sku": row.get("sku"),
                        "attributeEvidence": row.get("attributeEvidence", []),
                        "size": row.get("size"),
                        "stock": row.get("stock"),
                        "knownSpecs": row.get("knownSpecs", []),
                    }
                )
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            key = (
                str(row.get("product") or ""),
                str(row.get("variant") or ""),
                str(row.get("sku") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    def _requested_domains(self, plan_status: PlanStatus) -> set[str]:
        explicit = {intent for intent in plan_status.intent_classes if intent in {"stock", "weather", "news", "currency"}}
        if explicit:
            return explicit

        domains: set[str] = set()
        for step in plan_status.steps:
            domain = self._tool_domain(step.tool)
            if domain is not None:
                domains.add(domain)
        return domains or {"stock"}

    def _tool_domain(self, tool_name: str) -> str | None:
        if tool_name.startswith(("stock_", "resolver_")):
            return "stock"
        if tool_name.startswith("news_"):
            return "news"
        if tool_name.startswith(("currency_", "fx_")):
            return "currency"
        if tool_name.startswith("weather_"):
            return "weather"
        return None

    def _grounded_fallback_answer(
        self,
        clarification: ClarificationPayload | None,
        inventory_snapshot: dict[str, Any] | None,
        allow_snapshot_answer: bool,
    ) -> tuple[str, str, list[str]]:
        if clarification is not None:
            return self._default_incomplete_answer(True), "needs_clarification", []

        if inventory_snapshot and allow_snapshot_answer:
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

    def _allow_memory_reuse(self, session_state: SessionState) -> bool:
        scope = session_state.memory_scope
        return scope.transition != "topic_shift" or scope.allow_background_reference

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
            "Use stock_search to resolve candidate products before exact detail lookups.",
            "If catalogue search returns identifiers without enough user-facing detail, follow with stock_detail for each unresolved family.",
            "Do not call stock_extract_variant_evidence with variantId alone; supplement it with sku or id.",
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

    async def _append_autonomous_replan_steps(
        self,
        *,
        request_message: str,
        plan_status: PlanStatus,
        session_state: SessionState,
        traces: list[ToolTrace],
        limitations: list[str],
    ) -> str | None:
        if self._client is None:
            return None

        # Motivation vs Logic: retrieval loops were ending with `status=limited`
        # even when additional evidence paths still existed. This replan pass
        # asks the model to autonomously propose bounded follow-up steps based on
        # current plan+memo state instead of stopping early.
        available_tools = [tool.name for tool in self.tool_registry.list_tools()]
        payload = {
            "model": self.settings.foundry_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict JSON replan controller. "
                        "Reason from the request, current plan context, memoized evidence, and limitations. "
                        "Do not use hard-coded keyword routing. "
                        "When a stock_search call with a multi-word product phrase returns zero rows, "
                        "retry with a shorter distinctive product/model term inferred from the user's phrase or "
                        "prior evidence (example: if `charlie chair` returns no rows, try `charlie`). "
                        "If requested evidence is still missing but retrievable, return additional tool steps."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": request_message,
                            "plan_context": render_plan_context(
                                plan_status,
                                session_state.memo_cache,
                                request_message,
                                mode="compact",
                                active_subject=session_state.active_subject,
                                memory_scope=session_state.memory_scope,
                            ),
                            "traces": [trace.model_dump(mode="json") for trace in traces[-12:]],
                            "limitations": self._dedupe(limitations)[-12:],
                            "available_tools": available_tools,
                            "max_steps": self.settings.agent_replan_max_steps_per_round,
                            "output_schema": {
                                "should_replan": "boolean",
                                "reason": "string",
                                "steps": [{"tool": "string", "args": "object", "hypothesis": "string"}],
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 900,
        }

        try:
            response_payload = await self._post_chat_completion(payload, endpoint_name="/api/v1/query/replan")
            content = response_payload["choices"][0]["message"].get("content") or ""
            raw = self._parse_model_json_content(content, context="Replan")
        except (UpstreamServiceError, json.JSONDecodeError):
            return None

        if not isinstance(raw, dict) or not raw.get("should_replan"):
            return None

        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return None

        completed_step_ids = [step.id for step in plan_status.steps if step.status == "done"]
        dependency_ids = completed_step_ids[-1:] if completed_step_ids else []
        inserted_count = 0
        max_steps = max(1, self.settings.agent_replan_max_steps_per_round)
        for candidate in raw_steps[:max_steps]:
            if not isinstance(candidate, dict):
                continue
            tool_name = str(candidate.get("tool") or "").strip()
            args = candidate.get("args")
            if not tool_name or not isinstance(args, dict):
                continue
            if tool_name not in available_tools:
                continue
            if any(step.tool == tool_name and step.args == args for step in plan_status.steps):
                continue

            next_id = max((step.id for step in plan_status.steps), default=0) + 1
            plan_status.steps.append(
                PlanStep(
                    id=next_id,
                    name="autonomous replan retrieval",
                    tool=tool_name,
                    status="pending",
                    args=args,
                    depends_on=dependency_ids,
                    parallel_group=None,
                    hypotheses=[str(candidate.get("hypothesis") or raw.get("reason") or "Additional evidence required.")],
                    validation=None,
                )
            )
            inserted_count += 1

        if inserted_count == 0:
            return None

        reason = str(raw.get("reason") or "Requested evidence remained incomplete after the previous plan run.")
        return (
            f"Autonomous replan added `{inserted_count}` retrieval step(s) because coverage remained incomplete. "
            f"reason={reason}"
        )

    def _utc_now_iso(self) -> str:
        return datetime.now(tz=UTC).isoformat()
