from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from app.schemas import ActiveSubjectSnapshot, SessionMemoryScope, SessionState
from app.text.utils import lexical_overlap, normalize_text, significant_tokens


@dataclass(frozen=True)
class SubjectCandidate:
    label: str
    source: str


def derive_memory_scope(message: str, session_state: SessionState) -> SessionMemoryScope:
    compact = message.strip()
    if not compact:
        return SessionMemoryScope(transition="standalone")

    target_entity = _extract_target_entity(compact, session_state)
    active = session_state.active_subject
    bridge_signals: list[str] = []
    allow_background_reference = False

    if active is None or (not active.product_names and not active.identifiers and not active.label):
        return SessionMemoryScope(
            transition="standalone",
            target_entity=target_entity,
            allow_background_reference=allow_background_reference,
            bridge_signals=bridge_signals,
        )

    active_focus = " ".join(
        [active.label or ""] + list(active.product_names[:3]) + list(active.identifiers[:3])
    ).strip()
    overlap_with_active = lexical_overlap(compact, active_focus) if active_focus else 0.0
    message_tokens = significant_tokens(compact)
    short_follow_up = len(message_tokens) <= 2 and bool(message_tokens)
    secondary_target = _extract_secondary_target_entity(compact, active)
    distinct_target = bool(
        target_entity
        and _looks_like_specific_entity_label(target_entity)
        and _is_distinct_target(target_entity, active)
        and _is_confident_new_subject(target_entity, compact, active)
    )
    if (
        secondary_target
        and _looks_like_specific_entity_label(secondary_target)
        and _is_distinct_target(secondary_target, active)
        and _is_confident_new_subject(secondary_target, compact, active)
    ):
        target_entity = secondary_target
        distinct_target = True
    mentions_active_subject = _mentions_active_subject(compact, active)
    bridge_signals, allow_background_reference = _infer_bridge_signals(
        distinct_target=distinct_target,
        mentions_active_subject=mentions_active_subject,
        short_follow_up=short_follow_up,
        overlap_with_active=overlap_with_active,
    )

    # Root Cause vs Logic: anaphora used to force continuation before checking
    # whether the turn explicitly introduced a different subject (e.g. "that
    # Alto chair"), which caused stale identifier reuse from the prior product.
    # We now prioritize explicit distinct targets unless the user is asking for
    # additive/comparative carry-over.
    if distinct_target and not allow_background_reference and not mentions_active_subject:
        return SessionMemoryScope(
            transition="topic_shift",
            target_entity=target_entity,
            allow_background_reference=False,
            bridge_signals=bridge_signals,
        )

    explicit_continuation = (
        overlap_with_active >= 0.45
        or (short_follow_up and not distinct_target)
    )

    if explicit_continuation or allow_background_reference:
        return SessionMemoryScope(
            transition="continuation",
            target_entity=target_entity,
            allow_background_reference=allow_background_reference,
            bridge_signals=bridge_signals,
        )

    if distinct_target:
        return SessionMemoryScope(
            transition="topic_shift",
            target_entity=target_entity,
            allow_background_reference=False,
            bridge_signals=bridge_signals,
        )

    return SessionMemoryScope(
        transition="continuation",
        target_entity=target_entity,
        allow_background_reference=allow_background_reference,
        bridge_signals=bridge_signals,
    )


def apply_virtual_pruning(session_state: SessionState, scope: SessionMemoryScope) -> None:
    if scope.transition != "topic_shift":
        session_state.memory_scope = scope
        return

    # Motivation vs Logic: a topic shift should isolate new retrieval from stale
    # entity evidence while keeping durable session preferences and naming.
    if session_state.active_subject and _subject_has_content(session_state.active_subject):
        session_state.background_subjects = [session_state.active_subject, *session_state.background_subjects][:8]

    session_state.recent_product_names = []
    session_state.recent_resolved_identifiers = []
    session_state.last_candidate_list = []
    session_state.last_filters = {}
    session_state.current_plan = None
    session_state.plan_todo = []
    session_state.memo_cache.entries = []
    session_state.memo_cache.aggregates = {}
    session_state.plan_metadata.sorted_priorities = []
    session_state.plan_metadata.confidence_scores = {}
    session_state.plan_metadata.validation_findings = []
    session_state.conversation_history = []
    session_state.memory_scope = scope


def refresh_active_subject(
    session_state: SessionState,
    *,
    request_message: str,
    target_entity: str | None,
) -> None:
    subject = session_state.active_subject.model_copy(deep=True) if session_state.active_subject else ActiveSubjectSnapshot()
    if target_entity:
        subject.label = target_entity
        subject.source = "request"

    names = [name for name in session_state.recent_product_names if isinstance(name, str) and name.strip()]
    identifiers = [
        value
        for value in session_state.recent_resolved_identifiers
        if isinstance(value, str) and value.strip()
    ]
    if names:
        subject.product_names = _dedupe_limit([*names, *(subject.product_names or [])], limit=6)
    if identifiers:
        subject.identifiers = _dedupe_limit([*identifiers, *(subject.identifiers or [])], limit=8)

    if not subject.label:
        subject.label = target_entity or _extract_target_entity(request_message, session_state)
    if not subject.label and subject.product_names:
        subject.label = subject.product_names[0]
    if not subject.label and session_state.active_subject:
        subject.source = "session"
    elif names or identifiers:
        subject.source = "evidence"

    session_state.active_subject = subject if _subject_has_content(subject) else None


def _extract_target_entity(message: str, session_state: SessionState) -> str | None:
    candidates = _subject_candidates(session_state)
    best_label: str | None = None
    best_score = 0.0
    for candidate in candidates:
        score = lexical_overlap(message, candidate.label)
        if score > best_score:
            best_score = score
            best_label = candidate.label
    if best_label and best_score >= 0.45:
        return best_label

    tokens = significant_tokens(message)
    if not tokens:
        return None
    return " ".join(tokens[:4])


def _subject_candidates(session_state: SessionState) -> list[SubjectCandidate]:
    candidates: list[SubjectCandidate] = []
    for name in session_state.recent_product_names[:8]:
        compact = str(name).strip()
        if compact:
            candidates.append(SubjectCandidate(label=compact, source="recent_product"))
    for option in session_state.last_candidate_list[:8]:
        label = (option.label or "").strip()
        if label:
            candidates.append(SubjectCandidate(label=label, source="last_candidate"))
    if session_state.active_subject:
        for value in [session_state.active_subject.label, *(session_state.active_subject.product_names or [])]:
            compact = (value or "").strip()
            if compact:
                candidates.append(SubjectCandidate(label=compact, source="active_subject"))
    return candidates


def _infer_bridge_signals(
    *,
    distinct_target: bool,
    mentions_active_subject: bool,
    short_follow_up: bool,
    overlap_with_active: float,
) -> tuple[list[str], bool]:
    bridges: list[str] = []
    allow_background_reference = False

    # Motivation vs Logic: avoid brittle keyword allow-lists and infer carry-over
    # using subject structure; semantic comparison wording is handled by planner prompts.
    if distinct_target and mentions_active_subject:
        bridges.append("multi_subject")
        allow_background_reference = True
    elif not distinct_target and (short_follow_up or overlap_with_active >= 0.45):
        bridges.append("implicit_reference")

    return bridges, allow_background_reference


def _is_distinct_target(target_entity: str, active_subject: ActiveSubjectSnapshot) -> bool:
    target = normalize_text(target_entity)
    if not target:
        return False
    active_labels = [
        normalize_text(value)
        for value in [active_subject.label or "", *(active_subject.product_names or [])]
        if value
    ]
    if not active_labels:
        return True
    return all(label and target not in label and label not in target for label in active_labels)


def _mentions_active_subject(message: str, active_subject: ActiveSubjectSnapshot) -> bool:
    normalized_message = normalize_text(message)
    if not normalized_message:
        return False
    for value in [active_subject.label or "", *(active_subject.product_names or [])]:
        normalized_value = normalize_text(value)
        if normalized_value and normalized_value in normalized_message:
            return True
    return False


def _looks_like_specific_entity_label(value: str) -> bool:
    # Avoid treating attribute-only follow-ups (e.g. "its stock") as a new
    # subject; explicit pivots should usually contain at least two meaningful
    # tokens such as product family + category/name.
    meaningful_tokens = significant_tokens(value)
    return len(meaningful_tokens) >= 2


def _looks_like_named_entity_mention(message: str) -> bool:
    for match in re.finditer(r"\b[A-Z][a-z0-9]+\b", message):
        if match.start() > 0:
            return True
    return False


def _is_confident_new_subject(target_entity: str, message: str, active_subject: ActiveSubjectSnapshot) -> bool:
    if _looks_like_named_entity_mention(message):
        return True
    active_focus = " ".join([active_subject.label or "", *(active_subject.product_names or [])]).strip()
    if not active_focus:
        return False
    return lexical_overlap(target_entity, active_focus) >= 0.25


def _extract_secondary_target_entity(message: str, active_subject: ActiveSubjectSnapshot) -> str | None:
    normalized_message = normalize_text(message)
    if not normalized_message:
        return None
    active_labels = [
        normalize_text(value)
        for value in [active_subject.label or "", *(active_subject.product_names or [])]
        if value
    ]
    for active_label in sorted(active_labels, key=len, reverse=True):
        if not active_label or active_label not in normalized_message:
            continue
        remainder = normalized_message.replace(active_label, " ", 1).strip()
        tokens = significant_tokens(remainder)
        if len(tokens) >= 2:
            return " ".join(tokens[:4])
    return None


def _dedupe_limit(values: Iterable[str], *, limit: int) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = str(value).strip()
        if not compact:
            continue
        lowered = compact.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(compact)
        if len(deduped) >= limit:
            break
    return deduped


def _subject_has_content(subject: ActiveSubjectSnapshot) -> bool:
    return bool(subject.label or subject.product_names or subject.identifiers)
