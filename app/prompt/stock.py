# Motivation vs Logic: collect stock-related few-shots separately so we can
# tune inventory guidance without touching unrelated domains.
STOCK_EXAMPLES = """
STOCK Example 1:
User: Do we have the black floor?
Assistant: use stock.search_catalogue, inspect multiple candidates, then use resolver.disambiguate_candidates and ask a short clarification question.

STOCK Example 2:
User: Compare fl-la-la-lam-1-ble vs fl-da-dan
Assistant: use stock.compare_variants and answer only from the returned evidence.

STOCK Example 3:
User: Let me know the sizes and colour of any 4-5 items we have.
Assistant: use stock.search_catalogue to gather a small sample, then reuse each returned variants[].sku with stock.get_variant_evidence or stock.get_product to pull dimensions and any colour wording from variant or product names. Do not call variant evidence with variantId alone.

STOCK Example 4:
User: List all stock with sizes, colours, and specs in a table.
Assistant: prefer stock.inventory_snapshot for broad inventory tables because it returns compact variant-level evidence rows plus coverage. Use stock.search_catalogue or stock.get_product only when you need narrower follow-up resolution. When snapshot rows are already present, render them into a grouped Markdown table where each product appears once and variants are listed on separate rows below it. Translate raw stock fields into plain language (for example, "10 in stock, 8 available to hire"), and infer colour/finish only when product_name, variant_name, or variation_options make it explicit; otherwise say unknown. Your last assistant turn must include the full user-facing answer text, not only a <thought> block.
""".strip()
