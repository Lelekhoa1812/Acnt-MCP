from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.errors import InventoryNotFoundError, ParameterMappingError, UpstreamServiceError
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

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": render_system(
                    request=request.message,
                    session=session_state,
                    tools=self.tool_registry.list_tools(),
                ),
            },
            {"role": "user", "content": request.message},
        ]

        draft_answer = ""
        for step in range(self.settings.agent_max_steps):
            response = await self._complete(messages=messages, tools=self.tool_registry.tool_payloads())
            assistant = response["choices"][0]["message"]
            content = assistant.get("content")
            if content:
                thoughts.append(content.strip())

            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                draft_answer = (content or "").strip()
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
                    if result.trace:
                        traces.append(result.trace)
                    new_limitations, new_clarification, new_evidence = self._capture(result.data)
                    limitations.extend(new_limitations)
                    if new_clarification is not None:
                        clarification = new_clarification
                        session_state.last_candidate_list = new_clarification.options
                    if new_evidence:
                        for evidence in new_evidence:
                            self._update_session_with_evidence(session_state, evidence)
                        resolved_items = self._merge_evidence(resolved_items, new_evidence)
                    tool_content = json.dumps(result.data, ensure_ascii=False)
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

        envelope = await self._format(
            request=request.message,
            draft=draft_answer or "I need one more step or clarification before I can answer safely.",
            limitations=limitations,
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

    async def _complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if self._client is None:
            raise UpstreamServiceError(503, "Azure AI Foundry is not configured for `/api/v1/query`.")
        payload = {
            "model": self.settings.foundry_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_completion_tokens": 1400,
        }
        response = await self._client.post("/chat/completions", json=payload)
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text)
        return response.json()

    async def _format(
        self,
        request: str,
        draft: str,
        limitations: list[str],
        clarification: ClarificationPayload | None,
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
        response = await self._client.post("/chat/completions", json=payload)
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text)
        content = response.json()["choices"][0]["message"]["content"]
        try:
            raw = json.loads(content)
            if clarification and raw.get("clarification") is None:
                raw["clarification"] = clarification.model_dump(mode="json")
            return AgentEnvelope.model_validate(raw)
        except (json.JSONDecodeError, ValidationError):
            fallback_status = "needs_clarification" if clarification else "answered"
            return AgentEnvelope(
                status=fallback_status,
                answer=draft,
                limitations=self._dedupe(limitations),
                clarification=clarification,
            )

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

        if isinstance(data, dict) and "provenance" in data and "evidence_paths" in data:
            evidence.append(NormalizedEvidence.model_validate(data))
            return limitations, clarification, evidence

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "provenance" in item and "evidence_paths" in item:
                    evidence.append(NormalizedEvidence.model_validate(item))

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
