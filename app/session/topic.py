from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.schemas import ActiveSubjectSnapshot, SessionMemoryScope, SessionState
from app.text.utils import lexical_overlap, normalize_text, significant_tokens

_ADDITIVE_BRIDGES = {"also", "plus", "another", "additionally", "alongside", "including"}
_COMPARATIVE_BRIDGES = {"compare", "compared", "versus", "vs", "difference", "better", "worse", "than"}
_ANAPHORA_BRIDGES = {"it", "its", "them", "that", "those", "this", "same", "previous", "one", "ones"}


@dataclass(frozen=True)
class SubjectCandidate:
    label: str
    source: str


def derive_memory_scope(message: str, session_state: SessionState) -> SessionMemoryScope:
    compact = message.strip()
    if not compact:
        return SessionMemoryScope(transition="standalone")

    target_entity = _extract_target_entity(compact, session_state)
    bridge_signals = _bridge_signals(compact)
    allow_background_reference = any(signal in {"additive", "comparative"} for signal in bridge_signals)

    active = session_state.active_subject
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
    short_follow_up = len(message_tokens) <= 3 and bool(message_tokens)
    has_anaphora_bridge = "anaphora" in bridge_signals
    explicit_continuation = overlap_with_active >= 0.45 or has_anaphora_bridge or short_follow_up

    if explicit_continuation or allow_background_reference:
        return SessionMemoryScope(
            transition="continuation",
            target_entity=target_entity,
            allow_background_reference=allow_background_reference,
            bridge_signals=bridge_signals,
        )

    if target_entity and _is_distinct_target(target_entity, active):
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


def _bridge_signals(message: str) -> list[str]:
    tokens = set(significant_tokens(message))
    bridges: list[str] = []
    if tokens & _ADDITIVE_BRIDGES:
        bridges.append("additive")
    if tokens & _COMPARATIVE_BRIDGES:
        bridges.append("comparative")
    if tokens & _ANAPHORA_BRIDGES:
        bridges.append("anaphora")
    return bridges


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
