from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.prompt.currency import CURRENCY_EXAMPLES
from app.prompt.context import render_plan_context, render_session_context
from app.prompt.news import NEWS_EXAMPLES
from app.prompt.stock import StockPromptPolicy, build_stock_prompt_policy
from app.prompt.stock.furniture import furniture_capability_summary
from app.prompt.weather import WEATHER_EXAMPLES
from app.schemas import (
    ActiveSubjectSnapshot,
    MemoCache,
    PlanStatus,
    PlanStep,
    SessionMemoryScope,
    SessionState,
    ToolDefinition,
)

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

PLUGIN_EXAMPLES = {
    "weather": WEATHER_EXAMPLES,
    "currency": CURRENCY_EXAMPLES,
    "news": NEWS_EXAMPLES,
}

# Motivation vs Logic: keep the registry prompt compact while preserving the
# routing, latency, search, and variant safeguards that determine correctness.
SYSTEM_BEHAVIOR_RULES = [
    "Run planner -> retrieval -> validator -> composer in order.",
    "Handle each request as recursive discovery: decompose intent, retrieve evidence, follow identifiers, answer.",
    "Emit Plan Status JSON before the first retrieval tool call.",
    "Claude times out at 180s; target completion within 170s without sacrificing accuracy.",
    "For live external facts, retrieve first, validate second, answer last; use supplied policy metadata for static capability facts.",
    "Use exact identifiers before broad search; build tool args from schema + evidence; never hard-code unsupported filters or keyword lists.",
    "Use tools instead of guessing for live inventory, product, weather, news, and currency facts.",
    "Route named-category exploration to live inventory, not capability listing.",
    "If multiple products plausibly match, ask for clarification. If one product is confirmed, do not ask variant clarification; aggregate variants, respecting the 20-variant cap.",
    "For ambiguity, follow resolver payload: use explicit options for selectable sets; use total match count + short hints for large sets.",
    "Do not call `stock_disambiguate` after product confirmation; use `stock_detail` or `stock_snapshot` for variant details.",
    "For products with >5 variants, prefer `stock_snapshot` over `stock_compare` unless side-by-side compare is required.",
    "Harmonise stock tools are source of truth for live inventory; stock prompt policy is source of truth for supported department/category capability metadata.",
    "Weather/news/currency tools are auxiliary; keep vendor limits explicit.",
    "If asked about bookings, quotes, reservations, or event line items, say this workflow is not implemented in the current tool contract.",
    "For news: use `news_headlines` for live/regional coverage, `news_search` for broader research, and `news_sources` for outlets; ground claims in `topSources`, `topKeywords`, `publishedRange`, and `totalResults`.",
    "For news success claims, require `matchConfidence >= 0.4` and at least one `matchingArticle` with requested tokens; cite `matchingKeywords`.",
    "If search returns identifiers but not requested attributes, add a `stock_detail` next hop before answering.",
    "For product-name queries, use adaptive multi-pass search terms inferred from request and evidence.",
    "For high-volume searches, use paginated requests instead of one large request; prefer page size about 20 when args allow it, fetch pages in parallel when supported, and continue until no more results.",
    "For grouped most/least inventory questions by type, family, category, state, or all inventory, use `stock_aggregate`; the backend handles page-by-page aggregation automatically, so do not try to manage pagination in the prompt, and do not answer grouped totals from `stock_variant_rank`.",
    (
        "Use `stock_specs_rank` for complex stock/spec/dimension/pricing ranking questions, `stock_variant_rank` "
        "for intra-family variant resolution, and `stock_image` for Harmonise image retrieval/rendering. When "
        "rendering `stock_image`, follow its fallback order: encoded MCP image content first, then download the "
        "resolved URI locally and render that file, then activate the script/runtime environment before running "
        "the provided python3 preview script, then show only the best URI and explain any inline-rendering failure "
        "as an AI client technical issue."
    ),
    "For multi-item requests, execute one `stock_search` per item term; deduplicate multi-pass results by product id and SKU.",
    "For product-family requests, resolve candidate rows, fetch needed family details, and prefer one compact stock-detail path; do not stack `stock_snapshot`, `stock_detail`, and `stock_compare` unless each hop adds missing evidence.",
    "Avoid duplicate semantic retrieval once a tool result covers the requested stock attributes.",
    "For mixed intents, satisfy every requested domain; ground stock/pricing before currency conversion and keep independent utility branches parallel where possible.",
    (
        "If `stock_search` returns no rows for a multi-word product phrase, replan with a shorter "
        "distinctive product-name from the user's phrase or prior evidence (for example, if `charlie chair` "
        "returns no rows, try `charlie`) before reporting failure."
    ),
    "Stay within a reasonable latency budget: prefer answer-ready tools and bounded follow-up hops over raw retrieval chains.",
    "Never invent unsupported filters, rates, stock quantities, or historical facts.",
    "If a tool returns an upstream/auth limitation, explain it plainly and stop guessing.",
    "Keep answers scoped to requested attributes only.",
    "If user targets a specific variant, answer that variant only.",
    "If no specific variant/SKU is named, include resolved variant availability before concluding; for families/catalogues, summarize variants and deduplicate repeated values.",
    "Process/display at most 20 variants; if more exist, state the cap and ask a targeted narrowing question using variant name/options, SKU, product name, or sales note.",
    "Prefer product + variant names over SKU unless SKU is requested or needed to disambiguate.",
    (
        "No standalone color/finish field exists; match colour/finish via variants[].name, options, SKU, "
        "product name, or sales note, but never output color as a distinct attribute/key/column."
    ),
    "Final wording must match original user intent.",
    "Use user-friendly language; avoid internal runtime wording.",
    "In variant tables, group by product and list each variant on its own row.",
    "Never output raw stock fragments like `total=...` or `hirable=...`; rewrite as plain availability text.",
    "When `stock_search` returns SKUs, reuse them for follow-up detail lookups.",
    "Do not call `stock_get_variant_evidence` with variantId alone; pair with sku or product id, or use sku only.",
    "After a failing tool call, change the next args pattern using returned identifiers; do not repeat the same failing pattern.",
    "Do not finalize as answered or limited while requested attributes are still retrievable; autonomously replan and continue retrieval until coverage is complete or a true upstream limitation is proven.",
    "Derive regional availability from retrieved stock evidence fields (for example vic/nsw stock and hirable counts) once detail payloads are available.",
    "After tool calls finish, the next assistant message must include the full user-facing answer (after any <thought> block).",
    "Before every tool call, include a concise <thought> block.",
    "While the persisted plan still has open retrieval steps, prefer assistant messages that include tool_calls matching the next required step; avoid prose-only replies that force the runtime to advance the plan without your explicit tool selection.",
    "For a single named product or product line where the user only asks about stock, availability, hireability, or quantity, prefer the smallest retrieval chain: often one answer-ready hop (for example inventory snapshot with a focused search plus department, and category only when clearly implied); add a second hop only when the first cannot return matching rows or stock evidence.",
    "Do not schedule session or memory tools unless the user explicitly asks about session state, history, or prior context; availability questions should not depend on `session_state`.",
    "Keep planner/validator/cache/failure diagnostics in thought/debug output only; never surface them in final answer.",
    "Do not reveal hidden chain-of-thought; keep <thought> blocks concise and operational.",
]

THOUGHT_FORMAT = """
<thought>
goal: short operational goal
entity_guess: product | variant | category | department | weather | news | currency | unknown
intent_class: stock | weather | news | currency | mixed
strategy: exact lookup | catalogue search | metadata narrowing | clarification | external retrieval | replan
tool: tool name
args_draft: short JSON-like args
replan_strategy: null | {reason: "...", next_search_term: "..."}
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
- clarification: null or an object with keys question, options, total_matches, selection_mode, hints

If clarification is needed, set status to needs_clarification and make answer a concise user-facing clarification prompt.
When clarification.selection_mode is `select_option`, list the returned options so the user can pick one quickly.
When clarification.selection_mode is `refine_query`, mention the total match count and include short narrowing hints.
If the request is about bookings, quotes, reservations, or event line items, set status to out_of_scope and explain that the workflow is not yet implemented in the current tool contract.
Do not invent a new clarification topic unless the provided clarification payload already requires it.
Do not ask for data sources, tools, or setup details when the draft already came from completed tool retrieval.
If the draft says the run is partial or incomplete, prefer limited or error over answered.
Keep the answer scoped to the user's requested attributes; do not append unrelated fields.
If the user asked for a specific variant or SKU, answer only that variant.
If the user asked generally about stock availability for a product without specifying variants, include resolved variant availability before concluding.
If the user asked generally about a product family, cover resolved variants and deduplicate repeated values.
If product detail evidence includes regional stock numbers, state the requested regional availability directly instead of saying it cannot be confirmed.
If variant evidence is capped above 20, state that more variants exist and ask one targeted narrowing question using available fields such as variant name/options, SKU, product name, or sales note; user-facing examples may mention colour/finish, size/dimensions, style, fit, or material.
After a full product-family answer, optionally end with one short follow-up asking whether the user wants deeper detail on a specific variant.
Prefer product and variant names in prose; include SKUs only when requested or needed for disambiguation.
If colour or finish is only present in a variant’s `name`, keep it there in prose; do not invent a separate colour field in the answer.
Keep the final wording aligned to the user's original intent.
Never mention internal orchestration or debug artifacts in answer text, including plan steps, TODO status, memo/cache, validator outputs, cache-hit labels, tool names, argument payloads, or internal error traces.
If coverage is incomplete, explain the user impact in plain business language without technical diagnostics.
If the user asked to list or count supported stock departments or categories, allow a complete, natural-language answer when the draft is grounded in supplied capability or policy context.
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
    intent_classes: list[str] | tuple[str, ...] | None = None,
    context_mode: str = "normal",
) -> PromptRegistryPolicy:
    stock_policy = build_stock_prompt_policy(request, context_mode=context_mode)
    plugin_intents = _resolve_plugin_intents(request, intent_classes=intent_classes)
    route = PromptRegistryRoute(
        plugin_intents=plugin_intents,
    )
    include_stock_policy = _should_include_stock_policy(request, intent_classes=intent_classes, route=route)
    behavior_rules = _dedupe_preserving_order(
        [
            (
                "Route intent before planning: use stock tools for inventory/product requests, "
                "and use weather/news/currency plugins for those utility intents."
            ),
            *(stock_policy.behavior_rules if include_stock_policy else []),
            *_build_plugin_routing_rules(route),
        ]
    )
    examples = _build_registry_examples(
        route,
        stock_policy=stock_policy,
        context_mode=context_mode,
        include_stock_policy=include_stock_policy,
    )
    return PromptRegistryPolicy(route=route, behavior_rules=behavior_rules, examples=examples)


def _resolve_plugin_intents(
    request: str,
    *,
    intent_classes: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    # Motivation vs Logic: planner-provided intent classes are the primary
    # routing signal for modular scaling; lexical term matching remains only as
    # a compatibility fallback when no intent classes are available.
    if intent_classes:
        allowed = set(PLUGIN_EXAMPLES)
        resolved: list[str] = []
        for intent_class in intent_classes:
            normalized = str(intent_class).strip().lower()
            if normalized in allowed and normalized not in resolved:
                resolved.append(normalized)
        if resolved:
            return tuple(resolved)

    return ()


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
    include_stock_policy: bool,
) -> str:
    if context_mode == "compact":
        return ""

    selected: list[str] = []
    if include_stock_policy and stock_policy.examples:
        selected.append(stock_policy.examples)

    for plugin_name in route.plugin_intents:
        example = PLUGIN_EXAMPLES.get(plugin_name)
        if example:
            selected.append(example)

    return "\n\n".join(selected)


def _should_include_stock_policy(
    request: str,
    *,
    intent_classes: list[str] | tuple[str, ...] | None,
    route: PromptRegistryRoute,
) -> bool:
    if intent_classes:
        normalized = {str(value).strip().lower() for value in intent_classes if str(value).strip()}
        if normalized and normalized.issubset({"weather", "news", "currency"}):
            return False
    # Motivation vs Logic: stock policy should stay visible by default, but pure
    # plugin requests need a smaller prompt so unrelated furniture examples do
    # not compete with weather/news/currency routing.
    return True


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
    intent_classes: list[str] | tuple[str, ...] | None = None,
    context_mode: str = "normal",
) -> str:
    routed_policy = build_registry_prompt_policy(
        request,
        intent_classes=intent_classes,
        context_mode=context_mode,
    )
    behavior_rules = _dedupe_preserving_order(SYSTEM_BEHAVIOR_RULES + routed_policy.behavior_rules)
    examples_block = f"\nExamples:\n{routed_policy.examples}" if routed_policy.examples else ""
    return f"""
Role: Harmonise Orchestrator (stock-first Phase 1 runtime).

In scope:
{_render_bullets(CORE_SCOPE)}

Out of scope:
{_render_bullets(OUT_OF_SCOPE)}

Rules:
{_render_bullets(behavior_rules)}

Thought block format:
{THOUGHT_FORMAT}

User request:
{request}

Session summary:
{render_session_context(session, request, mode=context_mode)}

Tools:
{_tool_block(tools, context_mode=context_mode)}{examples_block}
""".strip()


def render_planner(
    request: str,
    session: SessionState,
    tools: list[ToolDefinition],
    context_mode: str = "normal",
) -> str:
    capability_context = json.dumps(furniture_capability_summary(), indent=2, ensure_ascii=False)
    return f"""
You are the planner phase for a tool-driven orchestration runtime.

Return STRICT JSON (no markdown) with exactly these top-level keys:
- goal: string
- intent_classes: array of strings
- steps: array of step objects; may be empty only for static capability answers grounded in Capability context
- memo: object
- status: string

Each non-empty step object must include:
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
- Before building steps, classify the user request into intent domains and emit `intent_classes` using one or more of: capability, stock, inventory_search, product_detail, comparison, follow_up, weather, news, currency, mixed, out_of_scope.
- Keep the DAG latency-aware: prefer bounded, answer-ready tools over long overlapping retrieval chains.
- If the request asks only about supported departments, supported categories, taxonomy counts, tool capability, or current stock scope, use the Capability context below, emit `intent_classes: ["capability"]`, set `steps: []`, and set `status: complete`. The composer will phrase the answer; your goal string may summarize what to cover (e.g. list all categories vs count only).
- Use Capability context both for those empty-step answers and, in stock plans, to pick consistent `departmentId`/`categoryId` and category reasoning without extra metadata tool calls when the mapping is clear from the JSON.
- For broad item types, plural nouns, or general classifications (for example coffee tables, stools, ottomans, dining furniture), plan `stock_list_category` first. The follow-up inventory/search/ranking step must use the returned categoryId; do not start with a plain text `stock_search` for these category-like requests.
- If the user names or implies one mapped category and asks to know more, explore, view options, see products/items, understand availability, or otherwise learn what is in that category, emit stock/inventory_search intent and plan live catalogue or inventory snapshot retrieval with the matched `categoryId`. Do not classify named-category exploration as capability-only.
- Keep specific product/model/SKU requests on the direct product evidence path, usually `stock_snapshot` with distinctive name tokens, without inserting `stock_list_category`.
- Do not plan `stock_snapshot`, `stock_search`, `stock_detail`, `stock_get_departments`, or `stock_get_categories` for pure capability/taxonomy/count questions that the Capability context already answers.
- For live product, variant, stock quantity, size, pricing, or availability questions, include at least one executable retrieval step.
- For a **narrow** ask—stock, availability, hireability, or quantity for **one** named product or product line (model name, series, or colloquial product label)—keep the plan **short**: prefer one answer-ready retrieval step when the tool args can be filled from the user phrase and capability context (for example a single `stock_snapshot` with `departmentId`, a non-empty `search` built from distinctive name tokens, and `categoryId` only when a category match is clear). Add a dependent second step only if the first hop would plausibly return no rows, ambiguous multi-product matches, or stock evidence gaps.
- For grouped most/least inventory questions by type, family, category, state, or all inventory, plan `stock_aggregate`; use `stock_variant_rank` only when the user explicitly asks for variant or SKU ranking inside a resolved family.
- Use `stock_specs_rank` for complex stock/spec/dimension/pricing ranking questions and `stock_image` when the user explicitly needs a Harmonise image.
- When the same turn only needs "is it in stock" or "how much stock" for a specific named item, do **not** add extra steps for colour, finish, or compare unless the user asked for those attributes.
- Do not plan `session_state` or other session tools unless the user explicitly asks about the session, memory, or prior turns.
- Build deterministic, executable DAG steps only.
- Use `depends_on` to represent prerequisite hops and `parallel_group` only for independent steps that can run in parallel.
- If a search step may return identifiers without enough user-facing detail, add a follow-up retrieval step; if naming may miss, include a broader or shorter fallback search text.
- For high-volume searches where args allow control, plan paginated requests around page size 20, split independent pages across `parallel_group` workers when supported, and continue until no more results; do not manually tune pagination for tools whose backend already paginates.
- If stock retrieval hits timeouts or partial coverage, add a dependent fallback step that narrows the search/filter rather than trying to tune pagination manually.
- For multi-word product phrases, make the fallback search: keep the distinctive product/model token(s) and remove generic descriptors.
- For product name discovery, plan adaptive search passes inferred from the user request and prior evidence, then deduplicate overlaps by product id/SKU before downstream steps.
- For multi-item requests, emit separate stock_search steps with one product target per step.
- When the user asks about colour/finish or variant selection, plan variant-level evidence from actual fields: variant name/options, SKU, product name, or sales note; do not assume a separate colour filter field.
- When catalogue rows include multiple variants, schedule only the detail retrieval needed to answer, capped at 20 variants unless the user narrows the request.
- For product-family requests, prefer one compact stock-detail path first; avoid planning both `stock_snapshot` and `stock_compare` unless the first path cannot satisfy the requested evidence.
- Do not plan duplicate semantic retrieval for the same stock family once a planned tool already returns size, stock, pricing, and variant evidence in one payload.
- For mixed-domain asks, include explicit steps for each requested domain (stock, currency, weather, news) and keep dependencies clear.
- For mixed stock + currency + news asks, plan stock retrieval before currency conversion, and keep unrelated utility branches parallel only when they do not depend on stock output.
- Never mark the plan complete while requested attributes are still missing and additional retrieval paths remain; append replan steps instead.
- If the session summary reports `memory_scope: topic_shift`, treat earlier entities as background only and plan from the new target entity; do not reuse unrelated identifiers.
- If the user message is a short affirmation or anaphora (e.g. yes, yeah, them, it) with no new product or query text, resolve the subject from the session memory summary above (`recent_product_names`, `recent_resolved_identifiers`, `last_candidate_list`, memo) and plan `stock_detail` or `stock_snapshot` with those identifiers—do not pass the raw affirmation string as a catalogue `search` term.
- Do not invent booking or quote tools; those workflows are not yet implemented in the current tool contract.
- Do not invent tools.
- Do not output extra keys.
- Set status to `complete` only when `steps` is empty for a static capability answer; otherwise set status to `in-progress`.
- Runtime does not inject keyword-based tool routing; your plan must fully drive tool selection and arguments.
- never rely on hard-coded keyword lists; derive tool choice and search/filter arguments from the user request, schemas, capability context, and retrieved evidence.

Capability context:
{capability_context}

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
    active_subject: ActiveSubjectSnapshot | None = None,
    memory_scope: SessionMemoryScope | None = None,
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
            active_subject=active_subject,
            memory_scope=memory_scope,
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
- expected_rows: integer or null — how many rows the step was expected to return
- actual_rows: integer or null — how many rows are present in the tool result
- findings: array of strings — factual observations about completeness or format
- ambiguity: array of strings — unresolved entity or data ambiguities
- missing_statistics: array of strings — expected fields that are absent
- confidence: number between 0 and 1 or null
- aggregates: object — any useful summary counts or totals derived from the result

Rules:
- Do NOT emit normalized_rows or normalized_evidence; those are reconstructed deterministically.
- Compare expected rows versus actual rows and mention mismatches in findings.
- If data appears partial, missing, or ambiguous, reflect it in ambiguity or missing_statistics.
- Keep findings factual and grounded in the provided tool payload sample.
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
    active_subject: ActiveSubjectSnapshot | None = None,
    memory_scope: SessionMemoryScope | None = None,
) -> str:
    payload = {
        "request": request,
        "plan_context": render_plan_context(
            plan,
            memo_cache,
            request,
            mode=context_mode,
            active_subject=active_subject,
            memory_scope=memory_scope,
        ),
        "limitations": limitations,
    }
    if "capability" in {intent.strip().lower() for intent in plan.intent_classes if intent.strip()}:
        payload["capability_context"] = furniture_capability_summary()
    return f"""
You are the composer phase.

Write the final user-facing answer only, aligned to the user's request and grounded in memoized evidence or supplied capability context.
Requirements:
- Do not mention plan steps, TODO status, memo/cache mechanics, validation counters, tool names, argument details, Redis/cache-hit statuses, or internal failed attempts.
- Do not mention debug payload sections or thought-block mechanics.
- If evidence is incomplete, describe only the user-facing impact in plain language.
- Keep the response scoped to the active subject in plan_context; mention other products only when the request explicitly compares or adds them.
- If variants are capped above 20, say more exist and ask one targeted narrowing question using available fields (variant name/options, SKU, product name); examples may include colour/finish, size/dimensions, style, fit, or material.
- When colour or finish is encoded only in a variant’s name, use that name in the answer; do not add a separate colour label or field.
- Do not invent facts.
- Do not call tools.
- For capability-only plans, answer only from `capability_context`: compose lists, counts, or short summaries to match the user (e.g. full category list, department scope, or how many routes exist). Do not fabricate routes beyond the JSON. When the user only needs part of the taxonomy, stay concise; when they ask for a full list, include the relevant names and ids from the reference.

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
