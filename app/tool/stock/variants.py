from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class VariantCapResult(Generic[T]):
    variants: list[T]
    limit: int
    total_variants: int

    @property
    def shown_variants(self) -> int:
        return len(self.variants)

    @property
    def omitted_variants(self) -> int:
        return max(0, self.total_variants - self.shown_variants)

    @property
    def is_capped(self) -> bool:
        return self.omitted_variants > 0


def cap_variants(
    variants: list[T],
    *,
    limit: int,
    sku_getter: Callable[[T], str | None],
    variant_id_getter: Callable[[T], str | None],
    required_skus: list[str | None] | None = None,
    required_variant_ids: list[str | None] | None = None,
) -> VariantCapResult[T]:
    # Motivation vs Logic: product families can have 100+ variants, so all detail
    # enrichment paths share one capping rule instead of each fan-out loop deciding
    # independently how many variants to hydrate.
    bounded_limit = max(1, limit)
    capped = list(variants[:bounded_limit])
    required_sku_set = _compact_set(required_skus or [])
    required_variant_id_set = _compact_set(required_variant_ids or [])
    if required_sku_set or required_variant_id_set:
        seen_skus = _compact_set(sku_getter(variant) for variant in capped)
        seen_variant_ids = _compact_set(variant_id_getter(variant) for variant in capped)
        for variant in variants[bounded_limit:]:
            sku = _compact(sku_getter(variant))
            variant_id = _compact(variant_id_getter(variant))
            sku_required = bool(sku and sku in required_sku_set and sku not in seen_skus)
            variant_id_required = bool(
                variant_id and variant_id in required_variant_id_set and variant_id not in seen_variant_ids
            )
            if not sku_required and not variant_id_required:
                continue
            capped.append(variant)
            if sku:
                seen_skus.add(sku)
            if variant_id:
                seen_variant_ids.add(variant_id)

    return VariantCapResult(
        variants=capped,
        limit=bounded_limit,
        total_variants=len(variants),
    )


def build_variant_cap_metadata(
    *,
    product_id: str | None,
    product_name: str | None,
    result: VariantCapResult[object],
) -> dict[str, int | str | None] | None:
    if not result.is_capped:
        return None
    return {
        "limit": result.limit,
        "totalVariants": result.total_variants,
        "shownVariants": result.shown_variants,
        "omittedVariants": result.omitted_variants,
        "productId": product_id,
        "productName": product_name,
    }


def _compact_set(values) -> set[str]:
    return {compact for value in values if (compact := _compact(value))}


def _compact(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    compact = value.strip()
    return compact or None
