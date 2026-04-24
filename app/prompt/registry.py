from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.prompt.currency import CURRENCY_EXAMPLES
from app.prompt.context import render_plan_context, render_session_context
from app.prompt.news import NEWS_EXAMPLES
from app.prompt.stock import StockPromptPolicy, build_stock_prompt_policy
from app.prompt.weather import WEATHER_EXAMPLES
from app.text.utils import normalize_text
from app.schemas import MemoCache, PlanStatus, PlanStep, SessionState, ToolDefinition

# Motivation vs Logic: prompt policy is now the single configurable control
# surface for reasoning, clarification, answer style, and tool behavior so the
# runtime no longer hard-codes user-intent rules in scattered modules.
CORE_SCOPE = [
    "inventory lookup",
    "product and variant resolution",
    "specifications and stock visibility",
    "side-by-side comparisons",
    "clarification when inventory matches are ambiguous",
    "external plugin exploration for weather, news, and currency APIs",
]

OUT_OF_SCOPE = [
    "bookings",
    "quotes",
    "reservations",
    "event line items",
]

PLUGIN_INTENT_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "weather",
        (
            "weather",
            "forecast",
            "temperature",
            "rain",
            "wind",
            "humidity",
        ),
    ),
    (
        "currency",
        (
            "currency",
            "exchange",
            "convert",
            "conversion",
            "fx",
            "forex",
            "usd",
            "aud",
            "eur",
            "gbp",
            "jpy",
        ),
    ),
    (
        "news",
        (
            "news",
            "headline",
            "headlines",
            "article",
            "articles",
            "source",
            "sources",
            "outlet",
            "outlets",
        ),
    ),
)

PLUGIN_EXAMPLES = {
    "weather": WEATHER_EXAMPLES,
    "currency": CURRENCY_EXAMPLES,
    "news": NEWS_EXAMPLES,
}

SYSTEM_BEHAVIOR_RULES = [
    "Operate in explicit planner -> retrieval -> validator -> composer phases.",
    "Treat every request as recursive discovery: decompose intent, retrieve evidence, follow identifiers, then answer.",
    "Always emit a structured Plan Status JSON before the first retrieval tool call.",
    "Retrieve first, verify second, answer last.",
    "Prefer exact identifiers before broad search.",
    "Construct tool arguments from the current schema and retrieved evidence; do not hard-code unsupported filters or attributes.",
    "Use tool calls instead of guessing.",
    "When ambiguity remains, ask clarification instead of picking a candidate silently.",
    "Treat Harmonise stock tools as the source of truth for inventory answers.",
    "Treat weather/news/currency tools as auxiliary plugin demonstrations and keep vendor limitations explicit.",
    "If the user asks about bookings, quotes, reservations, or event line items, explain that the workflow is not yet implemented in the current tool contract.",
    "For news inquiries, pick `news.headlines` for live-trending or regional coverage, `news.search` for broader research, and `news.sources` when the user asks about outlets; ground every claim in the tool's `topSources`, `topKeywords`, `publishedRange`, and `totalResults` fields while naming any coverage limits.",
    "For news evidence, only claim success when `matchConfidence` is healthy (>=0.4) and at least one `matchingArticle` contains the requested tokens; cite `matchingKeywords` to prove relevancy and surface the words that triggered the match.",
    "If an initial search returns identifiers but not the requested attributes, plan the next retrieval hop with `stock.get_product` or `stock.extract_variant_evidence` before composing the answer.",
    "Never invent unsupported filters, rates, stock quantities, or historical facts.",
    "If a tool returns an upstream limitation or auth error, explain it plainly and stop guessing.",
    "Keep answers scoped to the requested attributes; do not include extra stock/spec fields unless asked.",
    "If the user targets a specific variant directly, answer that variant only.",
    "If the user asks generally about a product catalogue or family, summarize all resolved variants and deduplicate repeated values.",
    "Prefer product and variant names over SKU unless SKU is explicitly requested or needed to disambiguate.",
    "Final answer wording must align with the original user request intent after all tool calls.",
    "Use plain, user-friendly language; avoid internal system wording such as retrieval/runtime/internal data-source explanations.",
    "For variant tables, group rows by product so the product name appears once and each variant is listed in its own row.",
    "Never output raw stock fragments like `total=...` or `hirable=...`; rewrite them as descriptive availability text.",
    "When stock.search_catalogue returns variant SKUs, reuse those SKUs for follow-up size, colour, and details lookups.",
    "Do not call stock.get_variant_evidence with variantId alone; pair variantId with sku or product id, or just use sku directly.",
    "If a tool call fails, do not repeat the same failing argument pattern. Adjust the next call using identifiers already returned by prior tools.",
    "When many products or variants are needed, prefer answer-ready tools that return compact evidence rows over repeating raw single-product lookups.",
    "For broad inventory questions, plan paginated catalogue search and optional department/category discovery; after tools return, answer with structured Markdown and name any incompleteness.",
    "When you are done calling tools, your very next assistant message must contain the full reply to the user in natural language after any <thought> block. Never end the run with only <thought> and no substantive answer.",
    "Before every tool call, include a concise <thought> block in assistant content.",
    "Keep planner, validator, cache, and failed-attempt diagnostics in thoughts/debug outputs only; never surface those internals in the final user-facing answer.",
    "Do not reveal hidden chain-of-thought; keep <thought> blocks operational and concise.",
]

THOUGHT_FORMAT = """
<thought>
goal: short operational goal
entity_guess: product | variant | category | department | weather | news | currency | unknown
strategy: exact lookup | catalogue search | metadata narrowing | clarification | external retrieval
tool: tool name
args_draft: short JSON-like args
risk: ambiguity | missing attribute | vendor limit | none
</thought>
""".strip()


@dataclass(frozen=True)
class PromptRegistryRoute:
    plugin_intents: tuple[str, ...]


@dataclass(frozen=True)
class PromptRegistryPolicy:
    route: PromptRegistryRoute
    behavior_rules: list[str]
    examples: str


FORMAT = """
Return a JSON object with these keys only:
- status: one of answered, needs_clarification, out_of_scope, limited, error
- answer: string
- limitations: array of strings
- clarification: null or an object with keys question and options

If clarification is needed, set status to needs_clarification and make answer a concise user-facing clarification prompt.
If the request is about bookings, quotes, reservations, or event line items, set status to out_of_scope and explain that the workflow is not yet implemented in the current tool contract.
Do not invent a new clarification topic unless the provided clarification payload already requires it.
Do not ask for data sources, tools, or setup details when the draft already came from completed tool retrieval.
If the draft says the run is partial or incomplete, prefer limited or error over answered.
Keep the answer scoped to the user's requested attributes; do not append unrelated fields.
If the user asked for a specific variant or SKU, answer only that variant.
If the user asked generally about a product family, cover all resolved variants and deduplicate repeated values in the response.
Prefer product and variant names in prose; include SKUs only when requested or needed for disambiguation.
Keep the final wording aligned to the user's original intent.
Never mention internal orchestration or debug artifacts in answer text, including plan steps, TODO status, memo/cache, validator outputs, cache-hit labels, tool names, argument payloads, or internal error traces.
If coverage is incomplete, explain the user impact in plain business language without technical diagnostics.
""".strip()


def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def build_registry_prompt_policy(
    request: str,
    *,
    context_mode: str = "normal",
) -> PromptRegistryPolicy:
    stock_policy = build_stock_prompt_policy(request, context_mode=context_mode)
    route = PromptRegistryRoute(
        plugin_intents=_detect_plugin_intents(request),
    )
    behavior_rules = _dedupe_preserving_order(
        [
            (
                "Route intent before planning: use stock tools for inventory/product requests, "
                "and use weather/news/currency plugins for those utility intents."
            ),
            *stock_policy.behavior_rules,
            *_build_plugin_routing_rules(route),
        ]
    )
    examples = _build_registry_examples(route, stock_policy=stock_policy, context_mode=context_mode)
    return PromptRegistryPolicy(route=route, behavior_rules=behavior_rules, examples=examples)


def _detect_plugin_intents(request: str) -> tuple[str, ...]:
    normalized = normalize_text(request)
    tokens = set(normalized.split())
    return tuple(
        name for name, terms in PLUGIN_INTENT_TERMS if _contains_any(normalized, tokens, terms)
    )


def _build_plugin_routing_rules(route: PromptRegistryRoute) -> list[str]:
    rules: list[str] = []
    if route.plugin_intents:
        plugin_labels = ", ".join(route.plugin_intents)
        rules.append(
            (
                f"Detected plugin intents: {plugin_labels}. Keep plugin retrieval scoped to matching "
                "tools and avoid using stock tools for pure plugin questions."
            )
        )
        rules.append(
            "If a single request mixes plugin and stock topics, satisfy both intents within one reply."
        )
    return rules


def _build_registry_examples(
    route: PromptRegistryRoute,
    *,
    stock_policy: StockPromptPolicy,
    context_mode: str,
) -> str:
    if context_mode == "compact":
        return ""

    selected: list[str] = []
    if stock_policy.examples:
        selected.append(stock_policy.examples)

    for plugin_name in route.plugin_intents:
        example = PLUGIN_EXAMPLES.get(plugin_name)
        if example:
            selected.append(example)

    return "\n\n".join(selected)


def _contains_any(normalized: str, tokens: set[str], terms: tuple[str, ...]) -> bool:
    for term in terms:
        if _contains_term(normalized, tokens, term):
            return True
    return False


def _contains_term(normalized: str, tokens: set[str], term: str) -> bool:
    term_tokens = term.split()
    if len(term_tokens) == 1:
        return term_tokens[0] in tokens
    return f" {term} " in f" {normalized} "


def _tool_block(tools: list[ToolDefinition], context_mode: str = "normal") -> str:
    # Motivation vs Logic: the chat-completion API already receives the full
    # structured tool schema, so the text prompt only needs a concise roster.
    # That keeps the prompt legible without duplicating large JSON schemas in
    # every request.
    rendered: list[str] = []
    for tool in tools:
        line = f"- {tool.name}: {tool.description}"
        if context_mode != "compact":
            arg_hint = _tool_args_hint(tool.input_schema)
            if arg_hint:
                line += f" (args: {arg_hint})"
        rendered.append(line)
    return "\n".join(rendered)


def _tool_args_hint(input_schema: dict[str, Any]) -> str:
    properties = input_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return ""

    required = input_schema.get("required")
    required_names = [str(name) for name in required] if isinstance(required, list) else []
    ordered = [name for name in required_names if name in properties]
    ordered.extend(name for name in properties if name not in ordered)
    return ", ".join(ordered[:8])


def render_system(
    request: str,
    session: SessionState,
    tools: list[ToolDefinition],
    context_mode: str = "normal",
) -> str:
    routed_policy = build_registry_prompt_policy(request, context_mode=context_mode)
    behavior_rules = _dedupe_preserving_order(SYSTEM_BEHAVIOR_RULES + routed_policy.behavior_rules)
    return f"""
You are the Harmonise Orchestrator for the stock-first Phase 1 runtime.

Scope:
{_render_bullets(CORE_SCOPE)}

Out of scope:
{_render_bullets(OUT_OF_SCOPE)}

Behavior rules:
{_render_bullets(behavior_rules)}

Thought format:
{THOUGHT_FORMAT}

Current user request:
{request}

Session memory summary:
{render_session_context(session, request, mode=context_mode)}

Available tools:
{_tool_block(tools, context_mode=context_mode)}

Few-shot guidance:
{routed_policy.examples}
""".strip()


def render_planner(
    request: str,
    session: SessionState,
    tools: list[ToolDefinition],
    context_mode: str = "normal",
) -> str:
    return f"""
You are the planner phase for a tool-driven orchestration runtime.

Return STRICT JSON (no markdown) with exactly these top-level keys:
- goal: string
- steps: array of step objects
- memo: object
- status: string

Each step object must include:
- id: integer (start at 1)
- name: string
- tool: one available tool name from the list below
- status: one of planned or pending
- args: object
- depends_on: array of earlier step ids
- parallel_group: integer or null
- hypotheses: array of strings
- validation: null

Rules:
- Always include at least one step.
- Build deterministic, executable DAG steps only.
- Use `depends_on` to represent prerequisite hops and `parallel_group` only for independent steps that can run in parallel.
- If a search step is likely to return identifiers without enough user-facing detail, add a follow-up retrieval step instead of assuming the search result is final.
- Do not invent booking or quote tools; those workflows are not yet implemented in the current tool contract.
- Do not invent tools.
- Do not output extra keys.
- Set status to in-progress.

Current user request:
{request}

Session memory summary:
{render_session_context(session, request, mode=context_mode)}

Available tools:
{_tool_block(tools, context_mode=context_mode)}
""".strip()


def render_validator(
    *,
    plan: PlanStatus,
    step: PlanStep,
    tool_name: str,
    tool_args: dict[str, object],
    tool_result: object,
    tool_trace: dict[str, object],
    memo_cache: MemoCache,
    context_mode: str = "normal",
) -> str:
    payload = {
        "plan_context": render_plan_context(
            plan,
            memo_cache,
            " ".join(
                value
                for value in [
                    plan.goal,
                    step.name,
                    tool_name,
                    json.dumps(tool_args, ensure_ascii=False, separators=(",", ":"), default=str),
                ]
                if value
            ),
            mode=context_mode,
        ),
        "step": step.model_dump(mode="json"),
        "tool_name": tool_name,
        "tool_args": tool_args,
        "tool_result": tool_result,
        "tool_trace": tool_trace,
    }
    return f"""
You are the validator phase for tool retrieval outputs.

Return STRICT JSON (no markdown) with exactly these keys:
- expected_rows: integer or null
- actual_rows: integer or null
- findings: array of strings
- ambiguity: array of strings
- missing_statistics: array of strings
- confidence: number between 0 and 1 or null
- normalized_rows: array of row objects
- normalized_evidence: array of evidence objects
- aggregates: object

Rules:
- Normalize rows/evidence for cache reuse.
- Compare expected rows versus actual rows and mention mismatches in findings.
- If data appears partial, missing, or ambiguous, reflect it in ambiguity or missing_statistics.
- Keep findings factual and grounded in the provided tool payload.
- Do not output extra keys.

Context:
{json.dumps(payload, indent=2, ensure_ascii=False)}
""".strip()


def render_composer(
    *,
    request: str,
    plan: PlanStatus,
    memo_cache: MemoCache,
    limitations: list[str],
    context_mode: str = "normal",
) -> str:
    payload = {
        "request": request,
        "plan_context": render_plan_context(plan, memo_cache, request, mode=context_mode),
        "limitations": limitations,
    }
    return f"""
You are the composer phase.

Write the final user-facing answer only, aligned to the user's request and grounded in memoized evidence.
Requirements:
- Do not mention plan steps, TODO status, memo/cache mechanics, validation counters, tool names, argument details, Redis/cache-hit statuses, or internal failed attempts.
- Do not mention debug payload sections or thought-block mechanics.
- If evidence is incomplete, describe only the user-facing impact in plain language.
- Do not invent facts.
- Do not call tools.

Context:
{json.dumps(payload, indent=2, ensure_ascii=False)}
""".strip()


def render_formatter(
    request: str,
    draft: str,
    limitations: list[str],
    clarification: dict[str, object] | None,
) -> str:
    payload = {
        "request": request,
        "draft_answer": draft,
        "limitations": limitations,
        "clarification": clarification,
    }
    return f"{FORMAT}\n\nContext:\n{json.dumps(payload, indent=2, ensure_ascii=False)}"
