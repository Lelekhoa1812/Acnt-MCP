from __future__ import annotations

import json
from typing import Any, Literal

from app.schemas import ConversationTurn, MemoCache, MemoEntry, PlanStatus, PlanStep, SessionState
from app.text.utils import lexical_overlap, significant_tokens

# Motivation vs Logic: prompt rendering now scores and chunks session memory so
# the model sees only the relevant slice instead of a raw session dump.
ContextMode = Literal["normal", "compact"]

MODE_SETTINGS: dict[ContextMode, dict[str, int]] = {
    "normal": {
        "history_turns": 4,
        "memo_entries": 4,
        "rows_per_table_chunk": 6,
        "history_block_chars": 1400,
        "entry_note_chars": 220,
        "step_arg_chars": 240,
    },
    "compact": {
        "history_turns": 2,
        "memo_entries": 2,
        "rows_per_table_chunk": 4,
        "history_block_chars": 900,
        "entry_note_chars": 160,
        "step_arg_chars": 180,
    },
}


def summarize_session_state(session: SessionState, request: str, mode: ContextMode = "normal") -> dict[str, Any]:
    settings = MODE_SETTINGS[mode]
    summary: dict[str, Any] = {
        "session": {
            "session_id": session.session_id,
            "session_name": session.session_name,
            "session_name_source": session.session_name_source,
            "name_assigned": session.name_assigned,
        },
        "working_memory": _summarize_working_memory(session),
    }

    plan_summary = _summarize_plan(
        plan=session.current_plan,
        fallback_steps=session.plan_todo,
        validation_findings=session.plan_metadata.validation_findings,
        confidence_scores=session.plan_metadata.confidence_scores,
        sorted_priorities=session.plan_metadata.sorted_priorities,
        request=request,
        mode=mode,
    )
    if plan_summary is not None:
        summary["plan"] = plan_summary

    memo_summary = _summarize_memo_cache(session.memo_cache, request, mode)
    if memo_summary is not None:
        summary["memo"] = memo_summary

    history_summary = _summarize_history(session.conversation_history, request, settings)
    if history_summary:
        summary["conversation"] = history_summary

    return summary


def render_session_context(session: SessionState, request: str, mode: ContextMode = "normal") -> str:
    summary = summarize_session_state(session, request, mode)
    return _render_session_summary(summary)


def summarize_plan_context(plan: PlanStatus, memo_cache: MemoCache, request: str, mode: ContextMode = "normal") -> dict[str, Any]:
    summary = {
        "plan": _summarize_plan(
            plan=plan,
            fallback_steps=[],
            validation_findings=[],
            confidence_scores={},
            sorted_priorities=[],
            request=request,
            mode=mode,
        ),
    }
    memo_summary = _summarize_memo_cache(memo_cache, request, mode)
    if memo_summary is not None:
        summary["memo"] = memo_summary
    return summary


def render_plan_context(plan: PlanStatus, memo_cache: MemoCache, request: str, mode: ContextMode = "normal") -> str:
    summary = summarize_plan_context(plan, memo_cache, request, mode)
    return _render_plan_summary(summary)


def _summarize_working_memory(session: SessionState) -> dict[str, Any]:
    return {
        "recent_product_names": list(session.recent_product_names[:5]),
        "recent_resolved_identifiers": list(session.recent_resolved_identifiers[:5]),
        "last_candidates": [_summarize_candidate(option) for option in session.last_candidate_list[:4]],
        "last_filters": _compact_value(session.last_filters, 240),
        "preferences": _compact_value(session.preferences, 240),
    }


def _summarize_candidate(option) -> dict[str, Any]:
    return {
        "candidate_id": option.candidate_id,
        "label": option.label,
        "confidence": round(option.confidence, 3),
        "matched_on": list(option.matched_on[:4]),
        "product_id": option.product_id,
        "variant_id": option.variant_id,
        "sku": option.sku,
    }


def _summarize_plan(
    *,
    plan: PlanStatus | None,
    fallback_steps: list[PlanStep],
    validation_findings: list[str],
    confidence_scores: dict[str, float],
    sorted_priorities: list[int],
    request: str,
    mode: ContextMode,
) -> dict[str, Any] | None:
    steps = list(plan.steps if plan is not None else fallback_steps)
    if not steps and plan is None:
        return None

    settings = MODE_SETTINGS[mode]
    selected_steps = _select_steps(steps, request, settings["history_turns"] + 1)
    if not selected_steps and steps:
        selected_steps = steps[: settings["history_turns"] + 1]

    step_payloads: list[dict[str, Any]] = []
    for step in selected_steps:
        step_payloads.append(_summarize_step(step, settings["step_arg_chars"]))

    open_steps = [item for item in step_payloads if item["status"] != "done"]
    completed_steps = [item for item in step_payloads if item["status"] == "done"]

    return {
        "goal": plan.goal if plan is not None else "session working memory",
        "status": plan.status if plan is not None else "in-progress",
        "steps": step_payloads,
        "open_steps": open_steps[: settings["history_turns"]],
        "completed_steps": completed_steps[: settings["history_turns"]],
        "priority_order": list(sorted_priorities[:8]),
        "validation_findings": list(validation_findings[:5]),
        "confidence_scores": _top_confidence_scores(confidence_scores, limit=5),
        "step_count": len(steps),
    }


def _select_steps(steps: list[PlanStep], request: str, limit: int) -> list[PlanStep]:
    if not steps:
        return []
    request_tokens = significant_tokens(request)
    scored: list[tuple[float, int, PlanStep]] = []
    total = len(steps)
    for index, step in enumerate(steps):
        focus = " ".join(
            value
            for value in [
                step.name,
                step.tool,
                json.dumps(step.args, ensure_ascii=False, separators=(",", ":"), default=str),
            ]
            if value
        )
        overlap = lexical_overlap(request, focus) if request_tokens else 0.0
        recency = (index + 1) / max(total, 1)
        score = overlap * 2.0 + recency * 0.15
        if step.status != "done":
            score += 0.2
        scored.append((score, index, step))

    top = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[:limit]
    selected_indices = sorted(index for _, index, _ in top)
    return [steps[index] for index in selected_indices]


def _summarize_step(step: PlanStep, arg_limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": step.id,
        "name": step.name,
        "tool": step.tool,
        "status": step.status,
        "args": _compact_value(step.args, arg_limit),
        "hypotheses": list(step.hypotheses[:3]),
    }
    if step.validation is not None:
        payload["validation"] = {
            "expected_rows": step.validation.expected_rows,
            "actual_rows": step.validation.actual_rows,
            "cache_status": step.validation.cache_status,
            "findings": list(step.validation.findings[:3]),
            "ambiguity": list(step.validation.ambiguity[:3]),
            "missing_statistics": list(step.validation.missing_statistics[:3]),
            "confidence": step.validation.confidence,
        }
    return payload


def _summarize_memo_cache(memo_cache: MemoCache, request: str, mode: ContextMode) -> dict[str, Any] | None:
    if not memo_cache.entries:
        return None

    settings = MODE_SETTINGS[mode]
    selected_entries = _select_entries(memo_cache.entries, request, settings["memo_entries"])
    entry_payloads = [_summarize_entry(entry, settings) for entry in selected_entries]
    tool_counts = _tool_counts(memo_cache.entries)
    aggregates = _compact_value(memo_cache.aggregates, 360)

    summary: dict[str, Any] = {
        "entry_count": len(memo_cache.entries),
        "tool_counts": tool_counts,
        "aggregates": aggregates,
        "selected_entries": entry_payloads,
    }
    return summary


def _select_entries(entries: list[MemoEntry], request: str, limit: int) -> list[MemoEntry]:
    if not entries:
        return []
    request_tokens = significant_tokens(request)
    scored: list[tuple[float, int, MemoEntry]] = []
    total = len(entries)
    for index, entry in enumerate(entries):
        focus = " ".join(
            value
            for value in [
                entry.tool,
                json.dumps(entry.args, ensure_ascii=False, separators=(",", ":"), default=str),
                _entry_label(entry),
            ]
            if value
        )
        overlap = lexical_overlap(request, focus) if request_tokens else 0.0
        recency = (index + 1) / max(total, 1)
        score = overlap * 2.0 + recency * 0.2
        scored.append((score, index, entry))

    top = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[:limit]
    selected_indices = sorted(index for _, index, _ in top)
    return [entries[index] for index in selected_indices]


def _summarize_entry(entry: MemoEntry, settings: dict[str, int]) -> dict[str, Any]:
    row_limit = settings["rows_per_table_chunk"]
    row_payloads = [_compact_row(row, 180) for row in entry.rows[: row_limit * 2]]
    evidence_payloads = [_compact_evidence(item, 180) for item in entry.evidence[: row_limit]]
    summary: dict[str, Any] = {
        "step_id": entry.step_id,
        "tool": entry.tool,
        "args": _compact_value(entry.args, settings["entry_note_chars"]),
        "row_count": len(entry.rows),
        "evidence_count": len(entry.evidence),
        "rows": row_payloads,
        "evidence": evidence_payloads,
    }
    if entry.aggregates:
        summary["aggregates"] = _compact_value(entry.aggregates, settings["entry_note_chars"])
    if entry.provenance:
        summary["provenance"] = _compact_value(entry.provenance, settings["entry_note_chars"])
    return summary


def _compact_row(row: dict[str, Any], limit: int) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("product", "variant", "sku", "size", "stock", "knownSpecs"):
        value = row.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "knownSpecs" and isinstance(value, list):
            compact[key] = [_truncate_block(str(item), 80) for item in value[:3] if item]
            continue
        compact[key] = _compact_value(value, limit)
    return compact


def _compact_evidence(evidence: dict[str, Any], limit: int) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("product", "variant", "sku", "product_name", "variant_name", "totalStock", "dimensions", "stock", "provenance"):
        value = evidence.get(key)
        if value in (None, "", [], {}):
            continue
        compact[key] = _compact_value(value, limit)
    return compact


def _tool_counts(entries: list[MemoEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.tool] = counts.get(entry.tool, 0) + 1
    return counts


def _entry_label(entry: MemoEntry) -> str:
    for collection in (entry.evidence, entry.rows):
        for item in collection:
            if not isinstance(item, dict):
                continue
            for key in ("label", "product", "variant", "product_name", "variant_name", "sku"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _top_confidence_scores(confidence_scores: dict[str, float], limit: int) -> dict[str, float]:
    items = sorted(
        (
            (str(key), float(value))
            for key, value in confidence_scores.items()
            if isinstance(value, (int, float))
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    return {key: round(value, 3) for key, value in items[:limit]}


def _render_session_summary(summary: dict[str, Any]) -> str:
    sections: list[str] = []

    session = summary.get("session") or {}
    if session:
        session_lines = ["Session memory:"]
        session_lines.append(f"- session_id: {session.get('session_id')}")
        if session.get("session_name"):
            name_source = session.get("session_name_source") or "unknown"
            session_lines.append(f"- session_name: {session.get('session_name')} ({name_source})")
        if session.get("name_assigned") is not None:
            session_lines.append(f"- name_assigned: {session.get('name_assigned')}")
        sections.append("\n".join(session_lines))

    working_memory = summary.get("working_memory") or {}
    if working_memory:
        sections.append(_render_working_memory(working_memory))

    plan = summary.get("plan")
    if plan:
        sections.append(_render_plan_summary({"plan": plan}))

    memo = summary.get("memo")
    if memo:
        sections.append(_render_memo_summary(memo))

    conversation = summary.get("conversation")
    if conversation:
        sections.append(_render_conversation_summary(conversation))

    return "\n\n".join(section for section in sections if section)


def _render_plan_summary(summary: dict[str, Any]) -> str:
    plan = summary.get("plan") or {}
    if not plan:
        return ""

    lines = ["Active plan:"]
    lines.append(f"- goal: {plan.get('goal')}")
    lines.append(f"- status: {plan.get('status')}")
    lines.append(f"- step_count: {plan.get('step_count')}")

    steps = plan.get("steps") or []
    if steps:
        lines.append("- steps:")
        lines.extend(_render_step_lines(steps))

    open_steps = plan.get("open_steps") or []
    if open_steps:
        lines.append("- open_steps:")
        lines.extend(_render_step_lines(open_steps))

    completed_steps = plan.get("completed_steps") or []
    if completed_steps:
        lines.append("- completed_steps:")
        lines.extend(_render_step_lines(completed_steps))

    priority_order = plan.get("priority_order") or []
    if priority_order:
        lines.append(f"- priority_order: {', '.join(str(item) for item in priority_order)}")

    validation_findings = plan.get("validation_findings") or []
    if validation_findings:
        lines.append("- validation_findings:")
        for finding in validation_findings:
            lines.append(f"  - {finding}")

    confidence_scores = plan.get("confidence_scores") or {}
    if confidence_scores:
        scores = ", ".join(f"{key}={value}" for key, value in confidence_scores.items())
        lines.append(f"- confidence_scores: {scores}")

    return "\n".join(lines)


def _render_step_lines(steps: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for step in steps:
        line = f"  - step {step.get('id')}: {step.get('name')} ({step.get('tool')}) status={step.get('status')}"
        args = step.get("args")
        if args not in (None, {}, []):
            line += f" args={_render_compact_value(args)}"
        validation = step.get("validation")
        if validation:
            line += (
                f" findings={len(validation.get('findings') or [])}"
                f" confidence={validation.get('confidence')}"
            )
        lines.append(line)
    return lines


def _render_working_memory(working_memory: dict[str, Any]) -> str:
    lines = ["Working memory:"]

    recent_products = working_memory.get("recent_product_names") or []
    if recent_products:
        lines.append(f"- recent_products: {', '.join(recent_products)}")

    recent_identifiers = working_memory.get("recent_resolved_identifiers") or []
    if recent_identifiers:
        lines.append(f"- recent_identifiers: {', '.join(recent_identifiers)}")

    candidates = working_memory.get("last_candidates") or []
    if candidates:
        lines.append("- last_candidates:")
        for candidate in candidates:
            candidate_bits = [candidate.get("label") or candidate.get("candidate_id")]
            for key in ("sku", "product_id", "variant_id"):
                value = candidate.get(key)
                if value:
                    candidate_bits.append(f"{key}={value}")
            if candidate.get("confidence") is not None:
                candidate_bits.append(f"confidence={candidate.get('confidence')}")
            lines.append(f"  - {'; '.join(bit for bit in candidate_bits if bit)}")

    last_filters = working_memory.get("last_filters")
    if last_filters not in (None, {}, []):
        lines.append(f"- last_filters: {_render_compact_value(last_filters)}")

    preferences = working_memory.get("preferences")
    if preferences not in (None, {}, []):
        lines.append(f"- preferences: {_render_compact_value(preferences)}")

    return "\n".join(lines)


def _render_memo_summary(memo: dict[str, Any]) -> str:
    lines = ["Memo digest:"]
    lines.append(f"- entry_count: {memo.get('entry_count')}")

    tool_counts = memo.get("tool_counts") or {}
    if tool_counts:
        counts = ", ".join(f"{key} x{value}" for key, value in tool_counts.items())
        lines.append(f"- tool_counts: {counts}")

    aggregates = memo.get("aggregates")
    if aggregates not in (None, {}, []):
        lines.append(f"- aggregates: {_render_compact_value(aggregates)}")

    selected_entries = memo.get("selected_entries") or []
    if selected_entries:
        lines.append("- selected_entries:")
        for entry in selected_entries:
            lines.extend(_render_entry_lines(entry))

    return "\n".join(lines)


def _render_entry_lines(entry: dict[str, Any]) -> list[str]:
    lines = [
        "  - "
        + "; ".join(
            bit
            for bit in [
                f"step_id={entry.get('step_id')}" if entry.get("step_id") is not None else "",
                f"tool={entry.get('tool')}" if entry.get("tool") else "",
                f"rows={entry.get('row_count')}" if entry.get("row_count") is not None else "",
                f"evidence={entry.get('evidence_count')}" if entry.get("evidence_count") is not None else "",
            ]
            if bit
        )
    ]

    args = entry.get("args")
    if args not in (None, {}, []):
        lines.append(f"    args: {_render_compact_value(args)}")

    rows = entry.get("rows") or []
    if rows:
        lines.extend(_render_row_table(rows))

    evidence = entry.get("evidence") or []
    if evidence:
        lines.append("    evidence:")
        for item in evidence[:3]:
            lines.append(f"      - {_render_compact_value(item)}")

    provenance = entry.get("provenance")
    if provenance not in (None, {}, []):
        lines.append(f"    provenance: {_render_compact_value(provenance)}")

    return lines


def _render_row_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []

    columns = ["product", "variant", "sku", "size", "stock", "knownSpecs"]
    chunk_size = MODE_SETTINGS["normal"]["rows_per_table_chunk"]
    total_chunks = max(1, (len(rows) + chunk_size - 1) // chunk_size)
    lines: list[str] = []
    for chunk_index, start in enumerate(range(0, len(rows), chunk_size), start=1):
        rows_slice = rows[start : start + chunk_size]
        lines.append(f"    table chunk {chunk_index}/{total_chunks}:")
        lines.append(_render_table_header(columns))
        lines.append(_render_table_separator(columns))
        for row in rows_slice:
            lines.append(_render_table_row(row, columns))
        if start + chunk_size < len(rows):
            lines.append(f"    ... {len(rows) - (start + chunk_size)} more rows")
    return lines


def _render_table_header(columns: list[str]) -> str:
    return "| " + " | ".join(columns) + " |"


def _render_table_separator(columns: list[str]) -> str:
    return "| " + " | ".join("---" for _ in columns) + " |"


def _render_table_row(row: dict[str, Any], columns: list[str]) -> str:
    cells = [_render_table_cell(_row_column_value(row, column)) for column in columns]
    return "| " + " | ".join(cells) + " |"


def _row_column_value(row: dict[str, Any], column: str) -> Any:
    if column == "knownSpecs":
        specs = row.get(column) or []
        if isinstance(specs, list):
            return "; ".join(str(spec) for spec in specs[:3] if spec)
        return specs
    return row.get(column)


def _render_table_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("|", "\\|")
    return text


def _render_conversation_summary(conversation: list[dict[str, Any]]) -> str:
    sections = ["Relevant conversation excerpts:"]
    for turn in conversation:
        sections.append(f"- {turn.get('role')}:")
        for block in turn.get("blocks") or []:
            rendered = str(block).strip()
            if rendered:
                sections.append(rendered)
        if turn.get("truncated"):
            sections.append("excerpt truncated to stay within prompt budget")
    return "\n\n".join(section for section in sections if section)


def _summarize_history(turns: list[ConversationTurn], request: str, settings: dict[str, int]) -> list[dict[str, Any]]:
    # Motivation vs Logic: delegate conversation chunking/selection to SummarizerAgent so the prompt
    # renderer stays focused on structure, and summarization heuristics live in one place.
    from app.agent.summarizer import summarize_history as _summarize_history_impl

    return _summarize_history_impl(turns, request, settings)


def _truncate_block(block: str, limit: int) -> str:
    if len(block) <= limit:
        return block
    return block[: max(0, limit - 1)].rstrip() + "…"


def _compact_value(value: Any, limit: int) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in list(value.items())[:12]:
            compact[str(key)] = _compact_value(item, max(40, limit // 2))
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(rendered) <= limit:
            return compact
        return _truncate_block(rendered, limit)
    if isinstance(value, list):
        compact_list = [_compact_value(item, max(40, limit // 2)) for item in value[:12]]
        rendered = json.dumps(compact_list, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(rendered) <= limit:
            return compact_list
        return _truncate_block(rendered, limit)
    if isinstance(value, str):
        return _truncate_block(value, limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    rendered = str(value)
    return _truncate_block(rendered, limit)


def _render_compact_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(value)
