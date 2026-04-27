from __future__ import annotations

from typing import Any

from app.text.stock.names import trailing_label_after_separator


def _colour_finish_display(row: dict[str, Any]) -> str:
    # Motivation vs Logic: colour is not a separate API field; use variant naming
    # so the column is not empty when names include a finish (e.g. "Chair - Black").
    direct = [x for x in (row.get("attributeEvidence") or []) if (x or "").strip()]
    if direct:
        return ", ".join(direct)
    variant = (row.get("variant") or "").strip()
    tail = trailing_label_after_separator(variant)
    if tail:
        return tail
    return variant or (row.get("product") or "").strip()


def render_inventory_snapshot_markdown(
    rows: list[dict[str, Any]],
    coverage: dict[str, Any] | None = None,
) -> str:
    # Motivation vs Logic: when the model fails to produce a final answer after
    # successful retrieval, this fallback renderer still provides a polished,
    # user-facing grouped table instead of raw technical payload fragments.
    include_other_specs = _has_known_specs(rows)
    columns: list[tuple[str, str, str | None]] = [
        ("Product", "product", None),
        ("Variant", "variant", "unknown"),
        ("SKU", "sku", "unknown"),
        ("Colour", "colour", "unknown"),
        ("Size", "size", "unknown"),
        ("Availability", "availability", "unknown"),
    ]
    if include_other_specs:
        columns.insert(-1, ("Other Specs", "other_specs", "unknown"))

    header = "| " + " | ".join(column[0] for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [
        "Here is the inventory view of the products and variants.",
        "",
        header,
        divider,
    ]

    for grouped_rows in _group_rows_by_product(rows):
        for variant_index, row in enumerate(grouped_rows):
            row_values: dict[str, str | None] = {
                "product": row.get("product") if variant_index == 0 else "",
                "variant": row.get("variant"),
                "sku": row.get("sku"),
                "colour": _colour_finish_display(row),
                "size": row.get("size"),
                "other_specs": _known_specs_display(row),
                "availability": row.get("stock"),
            }
            row_cells = [
                _cell(
                    row_values[column_key],
                    "unknown" if column_key == "product" and variant_index == 0 else (fallback or ""),
                )
                for _, column_key, fallback in columns
            ]
            lines.append("| " + " | ".join(row_cells) + " |")

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


def _known_specs_display(row: dict[str, Any]) -> str | None:
    specs = row.get("knownSpecs")
    if isinstance(specs, list):
        cleaned = [str(item).strip() for item in specs if str(item).strip()]
        return "; ".join(cleaned) if cleaned else None
    if isinstance(specs, str):
        normalized = specs.strip()
        return normalized or None
    return None


def _has_known_specs(rows: list[dict[str, Any]]) -> bool:
    return any(_known_specs_display(row) for row in rows)


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
