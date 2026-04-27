from __future__ import annotations

from dataclasses import dataclass

from app.prompt.stock.furniture import (
    FURNITURE_DEPARTMENT_ID,
    FURNITURE_EXAMPLES,
    furniture_capability_rules,
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
            "Inventory/catalogue/product questions: call stock tools only when you need real product evidence."
        ),
        (
            "Cheapest evidence first: capability policy + session memo (retrieved facts), metadata tools when available, "
            "then live catalogue/detail for product facts (stock evidence)."
        ),
        (
            "Smallest stock chain; if one family call already has sizes, options, pricing, and stock, "
            "do not add redundant stock steps."
        ),
        (
            "Single-product availability: prefer one `stock_inventory_snapshot` (departmentId, search from "
            "name tokens; categoryId only if FURNITURE_CATEGORY_ROUTES match is clear). Add search or a second "
            "hop only if the first hop cannot identify the product or stock."
        ),
        (
            "Do not use `session_get_state` for availability; resolve ids from user text, prompt memo, or prior tools."
        ),
        (
            "Colour/finish live in variant `name` or options only—no separate field; never invent one."
        ),
        (
            f"Limit catalogue/stock to {route.department_name} (`departmentId={route.department_id}`); "
            "state when other departments are out of scope."
        ),
        *furniture_capability_rules(),
        *furniture_department_rules(),
        *furniture_category_rules(),
    ]


def _build_examples(*, context_mode: str) -> str:
    if context_mode == "compact":
        return ""

    return FURNITURE_EXAMPLES
