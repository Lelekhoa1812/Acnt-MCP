from __future__ import annotations

import logging
from dataclasses import dataclass

from app.schemas import CandidateOption, ClarificationPayload, ProductListItemDto
from app.text.stock.query import build_query_components, QueryComponents
from app.text.utils import fuzzy_ratio, lexical_overlap, normalize_text, significant_tokens


@dataclass
class RankedCandidate:
    option: CandidateOption
    product: ProductListItemDto


class ResolverService:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def rank_candidates(
        self,
        query: str,
        products: list[ProductListItemDto],
        limit: int = 5,
    ) -> list[RankedCandidate]:
        normalized_query = normalize_text(query)
        if not normalized_query:
            return []

        product_names = [product.name for product in products if product.name]
        variant_names = [
            variant.name
            for product in products
            for variant in product.variants
            if variant.name
        ]
        components = build_query_components(
            query=query,
            product_names=product_names,
            variant_names=variant_names,
        )

        # Motivation vs Logic: a tiered pipeline lets us resolve semantic queries before opening the floodgates to broad keyword matching.
        levels = [
            self._level_one_matches(query, components, products),
            self._level_two_matches(query, components, products),
            self._level_three_matches(query, components, products),
        ]

        for matches in levels:
            if not matches:
                continue
            matches.sort(key=lambda candidate: candidate.option.confidence, reverse=True)
            return matches[:limit]

        return []

    def _passes_type_filter(self, product: ProductListItemDto, components: QueryComponents) -> bool:
        if not components.type_tokens:
            return True
        product_tokens = set(significant_tokens(product.name or ""))
        return any(token in product_tokens for token in components.type_tokens)

    def _level_one_matches(
        self,
        query: str,
        components: QueryComponents,
        products: list[ProductListItemDto],
    ) -> list[RankedCandidate]:
        matches: list[RankedCandidate] = []
        for product in products:
            if not product.variants or not self._passes_type_filter(product, components):
                continue
            for variant in product.variants:
                if self._matches_full_phrase(components.normalized_query, product, variant):
                    matches.append(
                        self._build_ranked_candidate(
                            product=product,
                            variant=variant,
                            confidence=0.98,
                            matched_on=["full_product_variant_phrase"],
                        )
                    )
        return matches

    def _matches_full_phrase(
        self,
        normalized_query: str,
        product: ProductListItemDto,
        variant,
    ) -> bool:
        if not product.name or not variant.name:
            return False

        candidate_texts = [
            f"{product.name} {variant.name}",
            f"{variant.name} {product.name}",
            f"{product.name} - {variant.name}",
            f"{variant.name} - {product.name}",
        ]
        for candidate in candidate_texts:
            if normalized_query == normalize_text(candidate):
                return True
        return False

    def _level_two_matches(
        self,
        query: str,
        components: QueryComponents,
        products: list[ProductListItemDto],
    ) -> list[RankedCandidate]:
        if not components.variant_tokens:
            return []

        matches: list[RankedCandidate] = []
        for product in products:
            if not self._passes_type_filter(product, components):
                continue
            variant = self._select_variant_by_tokens(product, components.variant_tokens)
            if not variant:
                continue

            variant_match_score = self._token_overlap_score(variant.name or "", components.variant_tokens)
            if variant_match_score <= 0:
                continue

            product_score = lexical_overlap(query, product.name or "") * 0.25
            product_score += fuzzy_ratio(query, product.name or "") * 0.15
            confidence = min(0.94, 0.4 + variant_match_score * 0.5 + product_score)
            matched_on = ["core_product_variant", "variant_descriptor"]
            matched_on.extend(components.variant_tokens)
            matches.append(
                self._build_ranked_candidate(
                    product=product,
                    variant=variant,
                    confidence=confidence,
                    matched_on=matched_on,
                )
            )
        return matches

    def _select_variant_by_tokens(
        self,
        product: ProductListItemDto,
        tokens: list[str],
    ):
        best_variant = None
        best_score = 0.0
        for variant in product.variants:
            score = self._token_overlap_score(variant.name or "", tokens)
            if score > best_score:
                best_score = score
                best_variant = variant
        return best_variant if best_score > 0 else None

    def _token_overlap_score(self, text: str, tokens: list[str]) -> float:
        if not text or not tokens:
            return 0.0
        text_tokens = set(significant_tokens(text))
        matches = [token for token in tokens if token in text_tokens]
        if not matches:
            return 0.0
        return len(matches) / len(tokens)

    def _level_three_matches(
        self,
        query: str,
        components: QueryComponents,
        products: list[ProductListItemDto],
    ) -> list[RankedCandidate]:
        matches: list[RankedCandidate] = []
        for product in products:
            if not self._passes_type_filter(product, components):
                continue
            for variant in product.variants:
                score, matched_on = self._compute_variant_score(query, product, variant, components)
                if score <= 0:
                    continue
                matches.append(
                    self._build_ranked_candidate(
                        product=product,
                        variant=variant,
                        confidence=score,
                        matched_on=matched_on,
                    )
                )
        return matches

    def _compute_variant_score(
        self,
        query: str,
        product: ProductListItemDto,
        variant,
        components: QueryComponents,
    ) -> tuple[float, list[str]]:
        normalized_query = normalize_text(query)
        product_name = product.name or ""
        variant_name = variant.name or ""
        combined_text = " ".join(part for part in [product_name, variant_name] if part)

        product_score = 0.0
        matched_on: list[str] = []
        if normalized_query and normalized_query == normalize_text(product_name):
            product_score += 0.45
            matched_on.append("exact_product_name")
        product_score += lexical_overlap(query, product_name) * 0.25
        product_score += fuzzy_ratio(query, product_name) * 0.15

        variant_score = 0.0
        variant_matches: list[str] = []
        if normalized_query and normalized_query == normalize_text(variant.sku or ""):
            variant_score += 0.95
            variant_matches.append("exact_sku")
        if normalized_query and normalized_query == normalize_text(variant_name):
            variant_score += 0.55
            variant_matches.append("exact_variant_name")
        if normalized_query and normalized_query == normalize_text(combined_text):
            variant_score += 0.75
            variant_matches.append("exact_product_variant_phrase")
        variant_score += lexical_overlap(query, variant_name) * 0.25
        variant_score += fuzzy_ratio(query, variant_name) * 0.15
        variant_score += fuzzy_ratio(query, variant.sku or "") * 0.05
        variant_score += lexical_overlap(query, combined_text) * 0.35
        variant_score += fuzzy_ratio(query, combined_text) * 0.2
        if components.variant_tokens:
            token_score = self._token_overlap_score(variant_name, components.variant_tokens)
            if token_score > 0:
                variant_score += 0.15 * token_score
                variant_matches.append("variant_descriptor")

        option_names = [
            option.name or ""
            for variation in product.variations
            for option in variation.options
            if option.id in set(variant.optionIds)
        ]
        if option_names:
            option_score = max(lexical_overlap(query, option_name) for option_name in option_names)
            if option_score > 0:
                variant_score += option_score * 0.1
                variant_matches.append("variation_option")

        if components.type_tokens and any(
            token in set(significant_tokens(product_name)) for token in components.type_tokens
        ):
            product_score += 0.05
            matched_on.append("type_alignment")

        if components.variant_tokens:
            matched_on.extend(token for token in components.variant_tokens if token not in matched_on)

        matched_on.extend(token for token in variant_matches if token not in matched_on)

        total_score = min(0.99, product_score + variant_score)
        return total_score, matched_on or ["lexical_overlap"]

    def _build_ranked_candidate(
        self,
        product: ProductListItemDto,
        variant,
        *,
        confidence: float,
        matched_on: list[str],
    ) -> RankedCandidate:
        product_name = (product.name or "").strip() or "Unnamed product"
        label = product_name
        candidate_id = product.id
        variant_id = None
        sku = None
        if variant is not None:
            variant_name = (variant.name or "").strip()
            if variant_name and normalize_text(variant_name) != normalize_text(product_name):
                label = f"{label} / {variant_name}"
            candidate_id = variant.id
            variant_id = variant.id
            sku = variant.sku
        return RankedCandidate(
            option=CandidateOption(
                candidate_id=candidate_id,
                label=label,
                confidence=round(confidence, 2),
                matched_on=matched_on,
                product_id=product.id,
                variant_id=variant_id,
                sku=sku,
                evidence_summary=f"departmentId={product.departmentId}, categoryId={product.categoryId}",
            ),
            product=product,
        )

    def needs_clarification(self, ranked_candidates: list[RankedCandidate]) -> bool:
        if not ranked_candidates:
            return False
        if len(ranked_candidates) == 1:
            return ranked_candidates[0].option.confidence < 0.58
        top = ranked_candidates[0].option.confidence
        second = ranked_candidates[1].option.confidence
        return top < 0.72 or (top - second) < 0.08

    def build_clarification(
        self,
        query: str,
        ranked_candidates: list[RankedCandidate],
        *,
        option_limit: int,
        total_matches: int,
        selection_threshold: int,
    ) -> ClarificationPayload:
        if total_matches <= selection_threshold:
            options = [candidate.option for candidate in ranked_candidates[:option_limit]]
            question = f"I found {total_matches} plausible matches for '{query}'. Which one did you mean?"
            return ClarificationPayload(
                question=question,
                options=options,
                total_matches=total_matches,
                selection_mode="select_option",
            )

        question = (
            f"I found {total_matches} plausible matches for '{query}'. "
            "Please narrow it down so I can return the exact item quickly."
        )
        return ClarificationPayload(
            question=question,
            options=[],
            total_matches=total_matches,
            selection_mode="refine_query",
            hints=[
                "Use a more specific product name.",
                "Add size or dimensions (length/width/height).",
                "Add colour or finish details.",
            ],
        )
