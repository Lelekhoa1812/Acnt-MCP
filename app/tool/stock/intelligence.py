from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas import NormalizedEvidence, ProductAttributeFilter


STOCK_FIELDS: dict[str, tuple[str, str]] = {
    "VIC": ("vicStock", "vicHirable"),
    "NSW": ("nswStock", "nswHirable"),
    "QLD": ("qldStock", "qldHirable"),
    "overall": ("totalStock", "totalHirable"),
}


@dataclass(frozen=True)
class RankedProductGroup:
    key: str
    label: str
    rows: list[NormalizedEvidence]


def filter_evidence_by_attributes(
    evidence_items: list[NormalizedEvidence],
    filters: list[ProductAttributeFilter],
) -> tuple[list[NormalizedEvidence], list[str]]:
    if not filters:
        return evidence_items, []

    matched: list[NormalizedEvidence] = []
    for evidence in evidence_items:
        if all(_matches_attribute_filter(evidence, attribute_filter) for attribute_filter in filters):
            matched.append(evidence)

    notes: list[str] = []
    if not matched:
        requested = ", ".join(f"{item.field}={item.value}" for item in filters)
        notes.append(
            "No variants matched the supplied attribute filters. "
            f"Try a broader search or verify the category/style attributes via stock_snapshot. Filters: {requested}."
        )
    return matched, notes


def rank_evidence_with_filters(
    evidence_items: list[NormalizedEvidence],
    *,
    metric: str,
    region: str,
    group_by: str,
    direction: str,
    limit: int,
    attribute_filters: list[ProductAttributeFilter] | None = None,
) -> tuple[list[NormalizedEvidence], list[dict[str, Any]], list[str]]:
    # Motivation vs Logic: stock specs ranking and intra-family variant ranking now
    # share the same filtering + ranking contract so behavior stays consistent
    # instead of drifting across two near-duplicate tool implementations.
    filtered_items, filter_notes = filter_evidence_by_attributes(evidence_items, attribute_filters or [])
    ranked_rows = rank_product_intelligence(
        filtered_items,
        metric=metric,
        region=region,
        group_by=group_by,
        direction=direction,
        limit=limit,
    )
    return filtered_items, ranked_rows, filter_notes


def rank_product_intelligence(
    evidence_items: list[NormalizedEvidence],
    *,
    metric: str,
    region: str,
    group_by: str,
    direction: str,
    limit: int,
) -> list[dict[str, Any]]:
    groups = _group_evidence(evidence_items, group_by)
    reverse = direction == "most"
    ranked = sorted(
        (row for row in (_rank_group(group, metric=metric, region=region, direction=direction) for group in groups) if row),
        key=lambda row: row["rankValue"],
        reverse=reverse,
    )
    rows: list[dict[str, Any]] = []
    for rank, row in enumerate(ranked[:limit], start=1):
        row["rank"] = rank
        rows.append(row)
    return rows


def metric_value(evidence: NormalizedEvidence, *, metric: str, region: str) -> float | int | None:
    if metric in {"stock", "hirable"}:
        stock_field, hirable_field = STOCK_FIELDS[region]
        return getattr(evidence.stock, stock_field if metric == "stock" else hirable_field)
    if metric in {"length", "width", "height"}:
        return getattr(evidence.dimensions, metric)
    if metric == "area":
        return _multiply_dimensions(evidence, ["length", "width"])
    if metric == "volume":
        return _multiply_dimensions(evidence, ["length", "width", "height"])
    if metric in {"cost", "replacementValue"}:
        return evidence.pricing.cost
    if metric in {"generalRate", "hireRate"}:
        return evidence.pricing.generalRate
    if metric == "expoRate":
        return evidence.pricing.expoRate
    return None


def metric_unit(metric: str) -> str | None:
    if metric in {"length", "width", "height"}:
        return "m"
    if metric == "area":
        return "m2"
    if metric == "volume":
        return "m3"
    if metric in {"cost", "replacementValue", "generalRate", "expoRate", "hireRate"}:
        return "AUD"
    return None


def _rank_group(
    group: RankedProductGroup,
    *,
    metric: str,
    region: str,
    direction: str,
) -> dict[str, Any] | None:
    valued_rows = [
        (evidence, value)
        for evidence in group.rows
        if (value := metric_value(evidence, metric=metric, region=region)) is not None
    ]
    if not valued_rows:
        return None

    if metric in {"stock", "hirable"}:
        rank_value = sum(value for _, value in valued_rows)
        semantic = "sum"
        contributing = sorted(valued_rows, key=lambda item: item[1], reverse=True)
    else:
        reverse = direction == "most"
        contributing = sorted(valued_rows, key=lambda item: item[1], reverse=reverse)
        rank_value = contributing[0][1]
        semantic = "best_variant" if direction == "most" else "lowest_variant"

    product_ids = {item.product_id for item in group.rows if item.product_id}
    category_ids = {item.categoryId for item in group.rows if item.categoryId}
    return {
        "group": group.label,
        "groupBy": _group_by_label(group.key),
        "region": region,
        "metric": metric,
        "rankValue": rank_value,
        "rankUnit": metric_unit(metric),
        "aggregation": semantic,
        "variantCount": len(group.rows),
        "productIds": sorted(product_ids),
        "categoryIds": sorted(category_ids),
        "variants": [_variant_row(evidence, value, metric=metric, region=region) for evidence, value in contributing],
        "missingMetricFields": _missing_metric_fields(group.rows, metric=metric, region=region),
    }


def _variant_row(evidence: NormalizedEvidence, rank_value: float | int, *, metric: str, region: str) -> dict[str, Any]:
    stock_field, hirable_field = STOCK_FIELDS[region]
    return {
        "product": evidence.product_name,
        "variant": evidence.variant_name,
        "sku": evidence.sku,
        "metricValue": rank_value,
        "stock": getattr(evidence.stock, stock_field),
        "hirable": getattr(evidence.stock, hirable_field),
        "totalStock": evidence.stock.totalStock,
        "totalHirable": evidence.stock.totalHirable,
        "dimensions": evidence.dimensions.model_dump(mode="json"),
        "pricing": evidence.pricing.model_dump(mode="json"),
        "variationOptions": evidence.variation_options,
        "salesNote": evidence.salesNote,
        "media": evidence.media.model_dump(mode="json"),
    }


def _group_evidence(evidence_items: list[NormalizedEvidence], group_by: str) -> list[RankedProductGroup]:
    groups: dict[str, RankedProductGroup] = {}
    for evidence in evidence_items:
        key, label = _group_key(evidence, group_by)
        if key not in groups:
            groups[key] = RankedProductGroup(key=key, label=label, rows=[])
        groups[key].rows.append(evidence)
    return list(groups.values())


def _group_key(evidence: NormalizedEvidence, group_by: str) -> tuple[str, str]:
    if group_by == "category":
        label = evidence.categoryId or "Uncategorised"
        return f"category:{label}", label
    if group_by == "department":
        label = str(evidence.departmentId) if evidence.departmentId is not None else "Unknown department"
        return f"department:{label}", label
    if group_by == "variant":
        label = evidence.variant_name or evidence.sku or "Unnamed variant"
        return f"variant:{evidence.variant_id or evidence.sku or label}", label
    label = evidence.product_name or "Unnamed product"
    return f"product:{evidence.product_id or label}", label


def _group_by_label(key: str) -> str:
    return key.split(":", 1)[0]


def _matches_attribute_filter(evidence: NormalizedEvidence, attribute_filter: ProductAttributeFilter) -> bool:
    candidates = _attribute_candidates(evidence, attribute_filter.field)
    needle = _normalize(attribute_filter.value)
    if not needle:
        return True
    for candidate in candidates:
        normalized_candidate = _normalize(candidate)
        if attribute_filter.matchMode == "equals" and normalized_candidate == needle:
            return True
        if attribute_filter.matchMode == "contains" and needle in normalized_candidate:
            return True
    return False


def _attribute_candidates(evidence: NormalizedEvidence, field: str) -> list[str]:
    if field == "variationOption":
        return [value for value in evidence.variation_options if value]
    if field == "variantName":
        return [evidence.variant_name or ""]
    if field == "productName":
        return [evidence.product_name or ""]
    if field == "salesNote":
        return [evidence.salesNote or ""]
    return []


def _normalize(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _multiply_dimensions(evidence: NormalizedEvidence, fields: list[str]) -> float | None:
    values: list[float] = []
    for field in fields:
        value = getattr(evidence.dimensions, field)
        if value is None:
            return None
        values.append(value)
    product = 1.0
    for value in values:
        product *= value
    return product


def _missing_metric_fields(evidence_items: list[NormalizedEvidence], *, metric: str, region: str) -> list[str]:
    missing: list[str] = []
    for evidence in evidence_items:
        if metric_value(evidence, metric=metric, region=region) is None:
            missing.append(f"{evidence.sku or evidence.variant_name or 'unknown'}:{metric}")
    return sorted(set(missing))
