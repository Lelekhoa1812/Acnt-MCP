from __future__ import annotations

ECOMMERCE_PROMPT = {
    "domain": "ecommerce",
    "intent_classes": ["ecommerce"],
    "tools": ["ebay_item_search", "ebay_item_detail", "ebay_category_tree"],
    "examples": """
E-Commerce Example:
User: Find live eBay listings for a Sony WH-1000XM5 and show the seller details.
Assistant: Use `ebay_item_search` first, then `ebay_item_detail` on the strongest item id to surface seller details, images, and availability.
""".strip(),
    "rules": [
        "Use eBay tools for live marketplace browsing, pricing, availability, and category discovery.",
        "Search first for product discovery, then use item detail for seller, images, and item metadata.",
        "Use category tree data only for taxonomy/navigation questions or marketplace hierarchy discovery.",
    ],
}
