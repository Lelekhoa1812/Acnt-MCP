from __future__ import annotations

from dataclasses import dataclass

from app.prompt.stock.furniture import (
    FURNITURE_DEPARTMENT_ID,
    FURNITURE_EXAMPLES,
    furniture_category_rules,
    furniture_department_rules,
)

# Motivation vs Logic: this module now owns stock-only prompt policy. Cross-tool
# orchestration belongs in `app.prompt.registry`, while the LLM should surface
# stock intent detection entirely from its prompt instructions within the
# Furniture-focused domain.


@dataclass(frozen=True)
class StockPromptRoute:
    department_name: str
    department_id: int


@dataclass(frozen=True)
class StockPromptPolicy:
    route: StockPromptRoute
    behavior_rules: list[str]
    examples: str


def build_stock_prompt_policy(request: str, *, context_mode: str = "normal") -> StockPromptPolicy:
    route = StockPromptRoute(department_name="Furniture", department_id=FURNITURE_DEPARTMENT_ID)
    rules = _build_routing_rules(route)
    examples = _build_examples(context_mode=context_mode)
    return StockPromptPolicy(route=route, behavior_rules=rules, examples=examples)

def _build_routing_rules(route: StockPromptRoute) -> list[str]:
    return [
        (
            "Treat user requests as inventory, catalogue, or product inquiries; use your reasoning to "
            "decide whether stock tooling is warranted before invoking any tools."
        ),
        (
            "Prefer the smallest stock-tool chain that can answer the request; once a product-family tool "
            "already returns variant sizes, options, pricing, and stock, do not plan redundant stock lookups."
        ),
        (
            f"Confine cataloguing and stock tool calls to the {route.department_name} department by "
            f"setting `departmentId={route.department_id}` and explaining any other departments the "
            "user mentions as unsupported."
        ),
        *furniture_department_rules(),
        *furniture_category_rules(),
    ]


def _build_examples(*, context_mode: str) -> str:
    if context_mode == "compact":
        return ""

    return FURNITURE_EXAMPLES
