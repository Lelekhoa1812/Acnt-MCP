from __future__ import annotations

from app.tool.stock.presenter import render_inventory_snapshot_markdown


def test_render_inventory_snapshot_markdown_omits_other_specs_when_unknown() -> None:
    rows = [
        {
            "product": "Alto Chair",
            "variant": "Alto Chair - Black",
            "sku": "fn-se-ch-alt-bla",
            "attributeEvidence": ["Black"],
            "size": "0.48 x 0.42 x 0.76 m",
            "knownSpecs": [],
            "stock": "Overall has 195 in stock, with 172 available for hire.",
        }
    ]

    rendered = render_inventory_snapshot_markdown(rows)

    assert "| Product | Variant | SKU | Colour / Finish Evidence | Size | Availability |" in rendered
    assert "| Product | Variant | SKU | Colour / Finish Evidence | Size | Other Specs | Availability |" not in rendered


def test_render_inventory_snapshot_markdown_includes_other_specs_when_known() -> None:
    rows = [
        {
            "product": "Alto Chair",
            "variant": "Alto Chair - Black",
            "sku": "fn-se-ch-alt-bla",
            "attributeEvidence": ["Black"],
            "size": "0.48 x 0.42 x 0.76 m",
            "knownSpecs": ["Stackable", "Indoor use"],
            "stock": "Overall has 195 in stock, with 172 available for hire.",
        }
    ]

    rendered = render_inventory_snapshot_markdown(rows)

    assert "| Product | Variant | SKU | Colour / Finish Evidence | Size | Other Specs | Availability |" in rendered
    assert "Stackable; Indoor use" in rendered
