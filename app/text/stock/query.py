from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.text.utils import normalize_text, significant_tokens


@dataclass
class QueryComponents:
    query: str
    normalized_query: str
    tokens: list[str]
    brand_tokens: list[str]
    type_tokens: list[str]
    variant_tokens: list[str]


def build_query_components(
    query: str,
    *,
    product_names: Iterable[str] | None = None,
    variant_names: Iterable[str] | None = None,
) -> QueryComponents:
    # Motivation vs Logic: decomposing the freeform query into reusable brand/type/variant buckets
    # keeps downstream ranking deterministic instead of relying on ad-hoc token checks spread across the resolver.
    normalized_query = normalize_text(query)
    tokens = significant_tokens(query)
    product_names = list(product_names or [])
    variant_names = list(variant_names or [])

    brand_candidates = _collect_brand_candidates(product_names)
    type_candidates = _collect_type_candidates(product_names)
    variant_candidates = _collect_variant_candidates(variant_names)

    brand_tokens: list[str] = []
    type_tokens: list[str] = []
    variant_tokens: list[str] = []
    unmatched: list[str] = []

    for token in tokens:
        if token in brand_candidates:
            brand_tokens.append(token)
        elif token in type_candidates:
            type_tokens.append(token)
        elif token in variant_candidates:
            variant_tokens.append(token)
        else:
            unmatched.append(token)

    variant_tokens.extend(token for token in unmatched if token not in variant_tokens)
    return QueryComponents(
        query=query,
        normalized_query=normalized_query,
        tokens=tokens,
        brand_tokens=brand_tokens,
        type_tokens=type_tokens,
        variant_tokens=variant_tokens,
    )


def _collect_brand_candidates(names: Iterable[str]) -> set[str]:
    candidates: set[str] = set()
    for name in names:
        tokens = significant_tokens(name)
        if tokens:
            candidates.add(tokens[0])
    return candidates


def _collect_type_candidates(names: Iterable[str]) -> set[str]:
    candidates: set[str] = set()
    for name in names:
        tokens = significant_tokens(name)
        if len(tokens) > 1:
            candidates.update(tokens[1:])
    return candidates


def _collect_variant_candidates(names: Iterable[str]) -> set[str]:
    candidates: set[str] = set()
    for name in names:
        candidates.update(significant_tokens(name))
    return candidates
