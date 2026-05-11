from __future__ import annotations

from copy import deepcopy
from typing import Any


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
    },
    "required": ["type", "message"],
    "additionalProperties": True,
}


def build_envelope_output_schema(
    data_schema: dict[str, Any] | None = None,
    *,
    compact: bool = True,
) -> dict[str, Any]:
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
    return {
        "type": "object",
        "description": (
            "Standard acnt-mcp tool envelope. Successful calls populate `data` and, "
            "where useful, `answer_ready`/`normalization_notes`. Failed calls populate "
            "`error` instead."
        ),
        "properties": properties,
        "additionalProperties": False,
    }


def compact_success_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    if not envelope:
        return envelope
    trimmed = dict(envelope)
    notes = trimmed.get("normalization_notes")
    if isinstance(notes, list) and not notes:
        trimmed.pop("normalization_notes", None)
    return trimmed


__all__ = ["build_envelope_output_schema", "compact_success_envelope"]
