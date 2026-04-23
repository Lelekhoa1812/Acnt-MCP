from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import (
    Settings,
    InventoryNotFoundError,
    ParameterMappingError,
    UpstreamServiceError,
)
from app.inventory.presenter import render_inventory_snapshot_markdown
from app.prompt import render_formatter, render_system
from app.schemas import (
    AgentQueryRequest,
    CandidateOption,
    ClarificationPayload,
    NormalizedEvidence,
    SessionState,
    ToolTrace,
)
from app.tool.registry import ToolRegistry


THOUGHT_BLOCK_PATTERN = re.compile(r"<thought>.*?</thought>", re.IGNORECASE | re.DOTALL)


class AgentEnvelope(BaseModel):
    status: str
    answer: str
    limitations: list[str] = Field(default_factory=list)
    clarification: ClarificationPayload | None = None


class AgentRun(AgentEnvelope):
    thoughts: list[str] = Field(default_factory=list)
    tool_trace: list[ToolTrace] = Field(default_factory=list)
    resolved_items: list[NormalizedEvidence] = Field(default_factory=list)


class AgentEngine:
    # Motivation vs Logic: this engine hands tool selection, clarification, and
    # response drafting to the Foundry model while keeping execution bounded by
    # explicit tool schemas, max-step limits, and post-tool JSON formatting.
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
        used_grounded_snapshot_fallback = False

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": render_system(
                    request=request.message,
                    session=session_state,
                    tools=self.tool_registry.list_tools(),
                ),
            },
        ]
        # Motivation vs Logic: local REST development does not inherit Claude
        # browser memory, so we replay the in-process session transcript before
        # the new prompt to preserve follow-up context across `/query` calls.
        messages.extend(self._conversation_history_messages(session_state))
        messages.append({"role": "user", "content": request.message})

        draft_answer = ""
        for step in range(self.settings.agent_max_steps):
            response = await self._complete(messages=messages, enable_tools=True)
            assistant = response["choices"][0]["message"]
            content = assistant.get("content")
            if content:
                thoughts.append(content.strip())

            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                draft_answer = self._extract_user_facing_answer(content)
                if not draft_answer:
                    limitations.append("The model returned an empty final assistant message after tool retrieval.")
                    status_hint = "limited"
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant.get("content"),
                    "tool_calls": tool_calls,
                }
            )

            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                raw_arguments = tool_call["function"].get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    parsed_args = {"_raw": raw_arguments}

                try:
                    result = await self.tool_registry.call_tool(
                        tool_name,
                        parsed_args,
                        session_id=session_state.session_id,
                        thought=(assistant.get("content") or "").strip(),
                    )
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
                    if new_evidence:
                        for evidence in new_evidence:
                            self._update_session_with_evidence(session_state, evidence)
                        resolved_items = self._merge_evidence(resolved_items, new_evidence)
                    tool_content = self._render_tool_content(result)
                except (InventoryNotFoundError, ParameterMappingError, UpstreamServiceError, ValueError) as exc:
                    error_trace = ToolTrace(
                        thought=(assistant.get("content") or "").strip(),
                        tool=tool_name,
                        args=parsed_args if isinstance(parsed_args, dict) else {"value": parsed_args},
                        status="error",
                        normalization_notes=[str(exc)],
                    )
                    traces.append(error_trace)
                    limitations.append(str(exc))
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

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_content,
                    }
                )
        else:
            draft_answer = "I reached the tool-step limit before I could finish safely."
            limitations.append("The agent hit the configured max-step limit.")
            status_hint = "limited"

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

        if used_grounded_snapshot_fallback:
            envelope = AgentEnvelope(
                status=status_hint,
                answer=draft_answer,
                limitations=self._dedupe(limitations),
                clarification=clarification,
            )
        else:
            try:
                envelope = await self._format(
                    request=request.message,
                    draft=draft_answer or self._default_incomplete_answer(clarification is not None),
                    limitations=limitations,
                    clarification=clarification,
                    fallback_status=status_hint,
                )
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
        return AgentRun(
            status=envelope.status,
            answer=envelope.answer,
            limitations=self._dedupe(limitations + envelope.limitations),
            clarification=envelope.clarification or clarification,
            thoughts=thoughts if request.includeThoughts else [],
            tool_trace=traces,
            resolved_items=resolved_items,
        )

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

    def _conversation_history_messages(self, session_state: SessionState) -> list[dict[str, str]]:
        history_messages: list[dict[str, str]] = []
        for turn in session_state.conversation_history:
            content = turn.content.strip()
            if not content:
                continue
            history_messages.append({"role": turn.role, "content": content})
        return history_messages

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
        try:
            raw = json.loads(content)
            if clarification and raw.get("clarification") is None:
                raw["clarification"] = clarification.model_dump(mode="json")
            return AgentEnvelope.model_validate(raw)
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

    def _render_tool_content(self, result) -> str:
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
