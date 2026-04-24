from __future__ import annotations

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
    return [
        (
            f"When the query targets `{route.name}`, use the attached description "
            f"({route.description.lower()}) to reason how user language maps to that "
            f"category and assign `categoryId={route.category_id}`."
        )
        for route in FURNITURE_CATEGORY_ROUTES
    ]


FURNITURE_EXAMPLES = """
FURNITURE Example 1:
User: Show me chairs in stock.
Assistant: classify this as a furniture stock request, then call stock.search_catalogue with departmentId=3 and categoryId=b7d70000-eacf-fc4c-c59a-08de7f19d85e.

FURNITURE Example 2:
User: What lounge options do we have?
Assistant: call stock.search_catalogue with departmentId=3 and categoryId=b7d70000-eacf-fc4c-359b-08de7f19d91e, then summarize returned variants.

FURNITURE Example 3:
User: Show me stools and electronics.
Assistant: handle stools via furniture stock tools with departmentId=3 and categoryId=b7d70000-eacf-fc4c-0a24-08de7f19d8d2, and clearly state electronics is unavailable because only Furniture is supported right now.
""".strip()
