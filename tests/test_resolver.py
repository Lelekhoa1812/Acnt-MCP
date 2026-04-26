from __future__ import annotations

import logging

from app.resolver import RankedCandidate, ResolverService
from app.schemas import CandidateOption, ProductListItemDto, ProductVariantDto


def _candidate(index: int) -> RankedCandidate:
    return RankedCandidate(
        option=CandidateOption(
            candidate_id=f"cand-{index}",
            label=f"Candidate {index}",
            confidence=0.8,
            matched_on=["lexical_overlap"],
            product_id=f"prod-{index}",
            variant_id=f"var-{index}",
            sku=f"sku-{index}",
        ),
        product=None,  # type: ignore[arg-type]
    )


def test_build_clarification_returns_selectable_options_for_small_sets() -> None:
    service = ResolverService(logger=logging.getLogger("test.resolver"))
    ranked = [_candidate(1), _candidate(2), _candidate(3)]

    payload = service.build_clarification(
        "alto chair",
        ranked,
        option_limit=10,
        total_matches=3,
        selection_threshold=10,
    )

    assert payload.selection_mode == "select_option"
    assert payload.total_matches == 3
    assert len(payload.options) == 3
    assert not payload.hints


def test_build_clarification_returns_refine_guidance_for_large_sets() -> None:
    service = ResolverService(logger=logging.getLogger("test.resolver"))
    ranked = [_candidate(index) for index in range(1, 6)]

    payload = service.build_clarification(
        "chair",
        ranked,
        option_limit=10,
        total_matches=24,
        selection_threshold=10,
    )

    assert payload.selection_mode == "refine_query"
    assert payload.total_matches == 24
    assert payload.options == []
    assert payload.hints


def _build_product_with_variant(
    product_id: str,
    product_name: str,
    variant_id: str,
    variant_name: str,
    category_id: str,
) -> ProductListItemDto:
    return ProductListItemDto(
        id=product_id,
        name=product_name,
        departmentId=1,
        categoryId=category_id,
        isActive=True,
        variations=[],
        variants=[
            ProductVariantDto(
                id=variant_id,
                name=variant_name,
                sku=f"{variant_id}-sku",
            )
        ],
    )


def test_rank_candidates_prefers_full_product_variant_phrase() -> None:
    service = ResolverService(logger=logging.getLogger("test.resolver"))
    product = _build_product_with_variant(
        product_id="prod-stool",
        product_name="Baxter Stool",
        variant_id="var-artichoke",
        variant_name="Artichoke",
        category_id="stools",
    )

    candidates = service.rank_candidates("Baxter Stool Artichoke", [product])
    assert candidates
    assert candidates[0].option.matched_on == ["full_product_variant_phrase"]
    assert candidates[0].option.label.startswith("Baxter Stool / Artichoke")


def test_rank_candidates_uses_variant_descriptor_when_order_differs() -> None:
    service = ResolverService(logger=logging.getLogger("test.resolver"))
    product = _build_product_with_variant(
        product_id="prod-stool",
        product_name="Baxter Stool",
        variant_id="var-artichoke",
        variant_name="Artichoke",
        category_id="stools",
    )

    candidates = service.rank_candidates("Artichoke Baxter", [product])
    assert candidates
    assert "core_product_variant" in candidates[0].option.matched_on
    assert "variant_descriptor" in candidates[0].option.matched_on


def test_rank_candidates_respects_type_filter() -> None:
    service = ResolverService(logger=logging.getLogger("test.resolver"))
    stool = _build_product_with_variant(
        product_id="prod-stool",
        product_name="Baxter Stool",
        variant_id="var-artichoke",
        variant_name="Artichoke",
        category_id="stools",
    )
    chair = _build_product_with_variant(
        product_id="prod-chair",
        product_name="Baxter Dining Chair",
        variant_id="var-walnut",
        variant_name="Walnut",
        category_id="chairs",
    )

    candidates = service.rank_candidates("Artichoke Baxter Stool", [stool, chair])
    assert candidates
    assert candidates[0].product.id == "prod-stool"
    assert all(candidate.product.id == "prod-stool" for candidate in candidates)
