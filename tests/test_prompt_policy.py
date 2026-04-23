from __future__ import annotations

from app.prompt.registry import render_formatter, render_system
from app.schemas import SessionState


def test_render_system_includes_scope_and_variant_policy_guards() -> None:
    prompt = render_system(
        request="What is the height of Alto Chair?",
        session=SessionState(session_id="policy-test"),
        tools=[],
    )
    assert "Keep answers scoped to the requested attributes" in prompt
    assert "If the user targets a specific variant directly, answer that variant only." in prompt
    assert "Prefer product and variant names over SKU" in prompt
    assert "Final answer wording must align with the original user request intent" in prompt


def test_formatter_contract_requires_intent_aligned_scope() -> None:
    formatter_prompt = render_formatter(
        request="Show the image for Alto Chair - Black",
        draft="Here is the image.",
        limitations=[],
        clarification=None,
    )
    assert "Keep the answer scoped to the user's requested attributes" in formatter_prompt
    assert "answer only that variant" in formatter_prompt
    assert "deduplicate repeated values" in formatter_prompt
    assert "aligned to the user's original intent" in formatter_prompt
