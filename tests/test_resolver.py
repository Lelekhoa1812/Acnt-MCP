from __future__ import annotations

import logging

from app.resolver import RankedCandidate, ResolverService
from app.schemas import CandidateOption


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
