from __future__ import annotations

import json
from dataclasses import dataclass

# Motivation vs Logic: furniture routing rules and category mappings are kept in
# this module so the stock orchestrator can enforce department/category behavior
# from prompt policy alone without hard-coding cloud API filters in tool code.
FURNITURE_DEPARTMENT_ID = 3


@dataclass(frozen=True)
class FurnitureCategoryRoute:
    name: str
    category_id: str
    description: str


@dataclass(frozen=True)
class FurnitureDepartmentCapability:
    name: str
    department_id: int
    description: str


FURNITURE_CATEGORY_ROUTES: tuple[FurnitureCategoryRoute, ...] = (
    FurnitureCategoryRoute(
        name="Furniture - Seating",
        category_id="b7d70000-eacf-fc4c-2dc5-08de7f19d859",
        description="General seating requests such as sofas, benches, and casual seats.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Seating - Chairs",
        category_id="b7d70000-eacf-fc4c-c59a-08de7f19d85e",
        description="Upright chairs and similar seats that emphasize formal or dining use.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Seating - Lounges",
        category_id="b7d70000-eacf-fc4c-359b-08de7f19d91e",
        description="Relaxed seating such as lounges, chaises, couches, and daybeds.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Seating - Ottomans",
        category_id="b7d70000-eacf-fc4c-462d-08de7f19d908",
        description="Footstools, ottomans, and low-lying accent seats.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Seating - Stools",
        category_id="b7d70000-eacf-fc4c-0a24-08de7f19d8d2",
        description="High or low stools including bar, counter, and task stools.",
    ),
    # Motivation vs Logic: department 3 inspection surfaces these additional categories, so prompts can assign the exact categoryId inside the furniture tool surface.
    FurnitureCategoryRoute(
        name="Furniture - Storage - Drawer Modules",
        category_id="b7d70000-eacf-fc4c-5efc-08de7f19da82",
        description="Mobile drawer modules and pedestals for organizing small items near seating or workspace setups.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Display - A-Frame Panels",
        category_id="b7d70000-eacf-fc4c-4b8c-08de7f19d9b1",
        description="Freestanding A-frame display panels used for wayfinding or signage around furniture zones.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Service - Drinks Trolleys",
        category_id="b7d70000-eacf-fc4c-3a41-08de7f1acc1e",
        description="Arc drinks trolleys that move beverages and supplies between service points.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Service - Food Station Counters",
        category_id="b7d70000-eacf-fc4c-4e5b-08de7f1c2101",
        description="Art Series food station counters that support plated service and catering prep close to seating.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Service - Bar Counters",
        category_id="b7d70000-eacf-fc4c-5041-08de7f1b1e96",
        description="Art Series service bar counters built for beverage service and hospitality staging.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Tables - Bar Height",
        category_id="b7d70000-eacf-fc4c-61b4-08de7f1acabd",
        description="Arc bar-height tables for standing hospitality zones, including models with integrated charging.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Tables - Dining",
        category_id="b7d70000-eacf-fc4c-e3e4-08de7f19d999",
        description="Arc dining tables designed for seated meals inside hospitality or breakout spaces.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Tables - Coffee",
        category_id="b7d70000-eacf-fc4c-f320-08de7f19d96e",
        description="Arc coffee tables suitable for lounges and informal seating groups.",
    ),
    FurnitureCategoryRoute(
        name="Furniture - Tables - Side",
        category_id="b7d70000-eacf-fc4c-a788-08de7f19d986",
        description="Arc side tables used beside sofas and lounge chairs for drinks or devices.",
    ),
)


FURNITURE_DEPARTMENT_CAPABILITIES: tuple[FurnitureDepartmentCapability, ...] = (
    FurnitureDepartmentCapability(
        name="Furniture",
        department_id=FURNITURE_DEPARTMENT_ID,
        description="The only stock department currently supported by this assistant.",
    ),
)


def _describe_route_name(name: str) -> tuple[str, ...]:
    normalized = name.replace("-", " ").replace(",", "").lower()
    tokens = []
    for part in normalized.split():
        cleaned = "".join(ch for ch in part if ch.isalnum())
        if cleaned and cleaned not in tokens:
            tokens.append(cleaned)
    return tuple(tokens)


def _build_furniture_intent_terms() -> tuple[str, ...]:
    seen: list[str] = []
    for route in FURNITURE_CATEGORY_ROUTES:
        for term in _describe_route_name(route.name):
            if term not in seen:
                seen.append(term)
    seen.append("furniture")
    return tuple(seen)


FURNITURE_INTENT_TERMS: tuple[str, ...] = _build_furniture_intent_terms()


def furniture_capability_summary() -> dict[str, object]:
    return {
        "supported_department_count": len(FURNITURE_DEPARTMENT_CAPABILITIES),
        "supported_departments": [
            {
                "name": department.name,
                "department_id": department.department_id,
                "description": department.description,
            }
            for department in FURNITURE_DEPARTMENT_CAPABILITIES
        ],
        "mapped_furniture_category_count": len(FURNITURE_CATEGORY_ROUTES),
        "mapped_furniture_categories": [
            {
                "name": route.name,
                "category_id": route.category_id,
                "description": route.description,
            }
            for route in FURNITURE_CATEGORY_ROUTES
        ],
    }


def furniture_capability_rules() -> list[str]:
    # Motivation vs Logic: the JSON reference is the contract; the model composes
    # lists, counts, and routing choices in natural language—no canned string answer.
    summary = furniture_capability_summary()
    compact_reference = json.dumps(summary, ensure_ascii=True, indent=2)
    return [
        f"FURNITURE_CAPABILITY_SUMMARY reference:\n{compact_reference}",
        (
            "This object is the authoritative list of supported departments, mapped category routes "
            "(names, category_id, descriptions), and counts. Ground capability answers and planning "
            "inferences only in this reference; do not call stock tools for pure taxonomy, scope, or count questions."
        ),
        (
            "Compose the user-facing reply yourself: match the request—e.g. full list, short overview, exact "
            "count, or which category best matches a user phrase—using natural wording, not a single fixed template."
        ),
        (
            "In stock, inventory_search, or mixed plans, use the same reference to choose `departmentId` and "
            "`categoryId` and to reason about category names; prefer explicit ids from the reference when the match is clear."
        ),
        (
            "`stock_snapshot` is for live product/variant inventory evidence only; do not use it for "
            "department/category capability listing or counts."
        ),
    ]


def furniture_department_rules() -> list[str]:
    return [
        f"Furniture is the only stock department currently supported; use `departmentId={FURNITURE_DEPARTMENT_ID}` for furniture stock tool calls.",
        (
            f"Set `departmentId={FURNITURE_DEPARTMENT_ID}` dynamically in each stock tool argument payload; "
            "do not rely on backend defaults or hard-coded function behavior."
        ),
        "If the user requests a non-furniture department, politely explain only Furniture is currently available.",
        "If the user requests Furniture together with other departments, fulfill the Furniture portion and call out unavailable departments clearly.",
    ]


def furniture_category_rules() -> list[str]:
    compact_reference = json.dumps(
        [
            {
                "name": route.name,
                "category_id": route.category_id,
                "description": route.description,
            }
            for route in FURNITURE_CATEGORY_ROUTES
        ],
        ensure_ascii=True,
        indent=2,
    )
    return [
        f"FURNITURE_CATEGORY_ROUTES reference:\n{compact_reference}",
        (
            "Resolve stock category filters by reasoning over each route name and description in "
            "FURNITURE_CATEGORY_ROUTES, then assign the most likely `categoryId`."
        ),
        (
            "If category confidence is uncertain, skip `categoryId` and prioritize name-driven "
            "`search` arguments to avoid false-negative exclusions."
        ),
        (
            "When the user asks to know more about one mapped category, treat it as live category "
            "inventory exploration: use the matched `categoryId` with stock inventory/catalogue tools "
            "so the answer describes actual products, variants, availability, and relevant details."
        ),
        # Motivation vs Logic: category reasoning should remind the model to enumerate variants when the request is about availability.
        (
            "When the user asks about item availability via a mapped category, ensure the response covers each resolved "
            "variant's stock before wrapping up."
        ),
    ]


FURNITURE_EXAMPLES = """
FURNITURE Example 1:
User: Show me chairs in stock.
Assistant: classify this as a furniture stock request, then call stock_search with departmentId=3 and categoryId=b7d70000-eacf-fc4c-c59a-08de7f19d85e.

FURNITURE Example 2:
User: What lounge options do we have?
Assistant: call stock_search with departmentId=3 and categoryId=b7d70000-eacf-fc4c-359b-08de7f19d91e, then summarize returned variants.

FURNITURE Example 3:
User: Show me stools and electronics.
Assistant: handle stools via furniture stock tools with departmentId=3 and categoryId=b7d70000-eacf-fc4c-0a24-08de7f19d8d2, and clearly state electronics is unavailable because only Furniture is supported right now.

FURNITURE Example 4:
User: Let me know more about the coffee table category.
Assistant: classify this as live category inventory exploration, then call stock_snapshot with departmentId=3 and categoryId=b7d70000-eacf-fc4c-f320-08de7f19d96e before summarizing the coffee table products and variants.

FURNITURE Example 5:
User: Is the Arc lounge chair in stock?
Assistant: treat as a single-product availability check; call stock_snapshot with departmentId=3 and search terms from the product name (e.g. the distinctive model tokens). Add categoryId only if the user clearly points at a mapped category; avoid extra search hops if the snapshot already returns rows with stock evidence. Include every returned variant (with colours/sizes) and state their availability before concluding.

FURNITURE Example 6:
User: Tell me if the Spencer chair is still available.
Assistant: treat as another single-product availability request without variant names; use stock_snapshot so each resolved variant's stock is captured, and mention every variant's availability in the final answer before finishing.
""".strip()
