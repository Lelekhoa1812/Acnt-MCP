from __future__ import annotations

from copy import deepcopy
from typing import Any

# Motivation vs Logic: MCP clients (Claude.ai, ChatGPT, Cursor) warn when a tool
# lacks an `outputSchema` because the model cannot reason about result shape
# without it. This module centralises a single envelope JSON Schema that mirrors
# what `McpToolAdapter._success_result` actually returns, so every registered
# tool can advertise a real schema without forcing each handler to declare one.
#
# The envelope is intentionally union-shaped: a successful call carries `data`
# plus a few optional summary fields, while a failure carries `error`. Keeping
# both shapes inside one schema avoids needing to flip schemas between success
# and error paths and matches MCP's `structuredContent` contract.

_GENERIC_DATA_SCHEMA: dict[str, Any] = {
    "description": "Tool-specific result payload. Shape depends on the tool.",
}

_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Populated only when the tool failed.",
    "properties": {
        "type": {"type": "string", "description": "Exception class name."},
        "message": {"type": "string", "description": "Human-readable error message."},
        "status_code": {
            "type": "integer",
            "description": "Upstream HTTP status code when the failure came from a remote service.",
        },
        "code": {"type": "string", "description": "Stable identity-error code, if applicable."},
    },
    "required": ["type", "message"],
    "additionalProperties": True,
}


def build_envelope_output_schema(
    data_schema: dict[str, Any] | None = None,
    *,
    compact: bool = True,
) -> dict[str, Any]:
    """Return a JSON Schema describing the MCP `structuredContent` envelope.

    When ``compact`` is True the schema only advertises the fields kept by
    :func:`compact_success_envelope`. When False, the full envelope shape
    (including orchestration coordination fields) is advertised.
    """

    properties: dict[str, Any] = {
        "data": data_schema if data_schema is not None else dict(_GENERIC_DATA_SCHEMA),
        "answer_ready": {
            "description": "Optional pre-formatted, model-ready summary string or object.",
        },
        "normalization_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short notes about how upstream data was normalised.",
        },
        "error": deepcopy(_ERROR_SCHEMA),
    }
    if not compact:
        properties["plan_status"] = {
            "type": "object",
            "description": "Internal plan/step coordination snapshot (verbose; omitted in compact mode).",
        }
        properties["memo_update"] = {
            "type": "object",
            "description": "Internal memo cache delta (verbose; omitted in compact mode).",
        }
        properties["validation"] = {
            "type": "object",
            "description": "Internal validation findings (verbose; omitted in compact mode).",
        }

    return {
        "type": "object",
        "description": (
            "Standard hth-mcp tool envelope. Successful calls populate `data` and, "
            "where useful, `answer_ready`/`normalization_notes`. Failed calls "
            "populate `error` instead."
        ),
        "properties": properties,
        "additionalProperties": False,
    }


# Internal orchestration fields that are useful for the REST `/query` flow but
# add significant token weight for external MCP clients (ChatGPT/Claude) calling
# tools directly. They are dropped in compact mode.
_ORCHESTRATION_FIELDS: tuple[str, ...] = ("plan_status", "memo_update", "validation")


def compact_success_envelope(
    envelope: dict[str, Any],
    *,
    drop_orchestration: bool = True,
) -> dict[str, Any]:
    """Trim an MCP success envelope so it does not bleed orchestration noise."""

    if not drop_orchestration:
        return envelope
    if not envelope:
        return envelope
    trimmed = {k: v for k, v in envelope.items() if k not in _ORCHESTRATION_FIELDS}
    # Drop empty `normalization_notes` so the envelope is genuinely smaller.
    notes = trimmed.get("normalization_notes")
    if isinstance(notes, list) and not notes:
        trimmed.pop("normalization_notes", None)
    return trimmed


__all__ = [
    "build_envelope_output_schema",
    "compact_success_envelope",
]
