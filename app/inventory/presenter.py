from __future__ import annotations

from typing import Any


def render_inventory_snapshot_markdown(
    rows: list[dict[str, Any]],
    coverage: dict[str, Any] | None = None,
) -> str:
    # Motivation vs Logic: when the LLM successfully selects the bulk snapshot
    # tool but then returns empty answer turns, this renderer lets the runtime
    # serialize the already-grounded rows into a user-facing table instead of
    # discarding a complete retrieval run.
    lines = [
        "I pulled a grounded inventory snapshot from the current Harmonise data.",
        "The `Colour / Finish Evidence` column only echoes names or option labels returned by inventory; if those fields do not make a colour explicit, it remains `unknown`.",
        "",
        "| Product | Variant | SKU | Colour / Finish Evidence | Size | Other Specs | Stock |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.get("product"), "unknown"),
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


def _coverage_lines(coverage: dict[str, Any]) -> list[str]:
    matched_products = coverage.get("matchedProducts")
    enriched_variants = coverage.get("enrichedVariants")
    matched_pages = coverage.get("matchedPages")

    summary_bits: list[str] = []
    if matched_products is not None:
        summary_bits.append(f"Matched products: {matched_products}")
    if enriched_variants is not None:
        summary_bits.append(f"Enriched variants: {enriched_variants}")
    if matched_pages is not None:
        summary_bits.append(f"Matched pages: {matched_pages}")

    lines: list[str] = []
    if summary_bits:
        lines.append("Coverage: " + "; ".join(summary_bits) + ".")

    for limitation in coverage.get("limitations", []):
        if limitation:
            lines.append(f"Limitation: {limitation}")

    return lines


def _cell(value: str | None, fallback: str) -> str:
    normalized = (value or "").strip() or fallback
    return normalized.replace("|", "\\|").replace("\n", " ")
