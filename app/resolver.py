from __future__ import annotations

import logging
from dataclasses import dataclass

from app.schemas import CandidateOption, ClarificationPayload, ProductListItemDto
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
        ranked: list[RankedCandidate] = []
        normalized_query = normalize_text(query)
        query_tokens = significant_tokens(query)

        for product in products:
            best_variant = None
            best_variant_score = 0.0
            matched_on: list[str] = []
            product_score = 0.0

            if normalized_query and normalized_query == normalize_text(product.name or ""):
                product_score += 0.45
                matched_on.append("exact_product_name")
            product_score += lexical_overlap(query, product.name or "") * 0.25
            product_score += fuzzy_ratio(query, product.name or "") * 0.15

            for variant in product.variants:
                variant_score = 0.0
                variant_matches: list[str] = []
                combined_text = " ".join(part for part in [product.name or "", variant.name or ""] if part)
                if normalized_query and normalized_query == normalize_text(variant.sku or ""):
                    variant_score += 0.95
                    variant_matches.append("exact_sku")
                if normalized_query and normalized_query == normalize_text(variant.name or ""):
                    variant_score += 0.55
                    variant_matches.append("exact_variant_name")
                if normalized_query and normalized_query == normalize_text(combined_text):
                    variant_score += 0.75
                    variant_matches.append("exact_product_variant_phrase")
                variant_score += lexical_overlap(query, variant.name or "") * 0.25
                variant_score += fuzzy_ratio(query, variant.name or "") * 0.15
                variant_score += fuzzy_ratio(query, variant.sku or "") * 0.05
                variant_score += lexical_overlap(query, combined_text) * 0.35
                variant_score += fuzzy_ratio(query, combined_text) * 0.2
                if query_tokens and set(query_tokens).issubset(set(significant_tokens(combined_text))):
                    variant_score += 0.25
                    variant_matches.append("full_token_coverage")

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

                if query_tokens and all(token in normalize_text(product.name or "") for token in query_tokens):
                    variant_score += 0.1
                if variant_score > best_variant_score:
                    best_variant_score = variant_score
                    best_variant = variant
                    matched_on = variant_matches

            total_score = min(0.99, product_score + best_variant_score)
            if total_score <= 0:
                continue

            product_name = (product.name or "").strip() or "Unnamed product"
            label = product_name
            candidate_id = product.id
            variant_id = None
            sku = None
            if best_variant is not None:
                variant_name = (best_variant.name or "").strip()
                candidate_variant_label = self._format_variant_label_for_candidate(
                    variant_name, product_name
                )
                if candidate_variant_label:
                    label = f"{label} / {candidate_variant_label}"
                candidate_id = best_variant.id
                variant_id = best_variant.id
                sku = best_variant.sku

            ranked.append(
                RankedCandidate(
                    option=CandidateOption(
                        candidate_id=candidate_id,
                        label=label,
                        confidence=round(total_score, 2),
                        matched_on=matched_on or ["lexical_overlap"],
                        product_id=product.id,
                        variant_id=variant_id,
                        sku=sku,
                        evidence_summary=f"departmentId={product.departmentId}, categoryId={product.categoryId}",
                    ),
                    product=product,
                )
            )

        ranked.sort(key=lambda candidate: candidate.option.confidence, reverse=True)
        return ranked[:limit]

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

    def _format_variant_label_for_candidate(
        self,
        variant_name: str | None,
        product_name: str | None,
    ) -> str | None:
        # Motivation vs Logic: surface only the meaningful suffix (colour/finish)
        # instead of repeating the product name in candidate labels.
        label = (variant_name or "").strip()
        if not label:
            return None
        product = (product_name or "").strip()
        normalized_label = normalize_text(label)
        normalized_product = normalize_text(product)
        if product and normalized_label.startswith(normalized_product):
            trimmed = label[len(product) :].strip()
            trimmed = trimmed.lstrip(" -/").strip()
            if trimmed:
                return trimmed
        if normalized_label == normalized_product:
            return None
        return label
