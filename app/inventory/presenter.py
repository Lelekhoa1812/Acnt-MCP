from __future__ import annotations

from typing import Any


def render_inventory_snapshot_markdown(
    rows: list[dict[str, Any]],
    coverage: dict[str, Any] | None = None,
) -> str:
    # Motivation vs Logic: when the model fails to produce a final answer after
    # successful retrieval, this fallback renderer still provides a polished,
    # user-facing grouped table instead of raw technical payload fragments.
    lines = [
        "Here is a grouped inventory view so variants are easier to compare under each product.",
        "Colour / finish is inferred from product and variant naming; if no colour is explicit, it is shown as `unknown`.",
        "",
        "| Product | Variant | SKU | Colour / Finish Evidence | Size | Other Specs | Availability |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    for grouped_rows in _group_rows_by_product(rows):
        for variant_index, row in enumerate(grouped_rows):
            product_value = row.get("product") if variant_index == 0 else ""
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(product_value, "unknown" if variant_index == 0 else ""),
                        _cell(row.get("variant"), "unknown"),
                        _cell(row.get("sku"), "unknown"),
                        _cell(", ".join(row.get("attributeEvidence", [])), "unknown"),
                        _cell(row.get("size"), "unknown"),
                        _cell("; ".join(row.get("knownSpecs", [])), "unknown"),
                        _cell(row.get("stock"), "unknown"),
                    ]
                )
                + " |"
            )

    coverage_lines = _coverage_lines(coverage or {})
    if coverage_lines:
        lines.extend(["", *coverage_lines])

    return "\n".join(lines)


def _group_rows_by_product(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped_rows: list[list[dict[str, Any]]] = []
    grouped_lookup: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = ((row.get("product") or "").strip() or "unknown").casefold()
        group = grouped_lookup.get(key)
        if group is None:
            group = []
            grouped_lookup[key] = group
            grouped_rows.append(group)
        group.append(row)
    return grouped_rows


def _coverage_lines(coverage: dict[str, Any]) -> list[str]:
    enriched_products = coverage.get("enrichedProducts")
    enriched_variants = coverage.get("enrichedVariants")

    summary_bits: list[str] = []
    if enriched_products:
        summary_bits.append(
            f"{enriched_products} product{'s' if enriched_products != 1 else ''} expanded"
        )
    if enriched_variants:
        summary_bits.append(
            f"{enriched_variants} variant{'s' if enriched_variants != 1 else ''} listed"
        )

    lines: list[str] = []
    # if summary_bits:
    #     # Motivation vs Logic: explain what the user is looking at instead of dumping backend counters.
    #     lines.append("Snapshot summary: " + " and ".join(summary_bits) + ".")

    for limitation in coverage.get("limitations", []):
        if limitation:
            lines.append(f"Note: {limitation}")

    return lines


def _cell(value: str | None, fallback: str) -> str:
    normalized = (value or "").strip() or fallback
    return normalized.replace("|", "\\|").replace("\n", " ")
