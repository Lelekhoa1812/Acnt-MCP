from __future__ import annotations

import hashlib
import re

_MCP_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_NON_MCP_TOOL_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")


def normalize_mcp_tool_name(name: str) -> str:
    # Root Cause vs Logic: the MCP deployment validator only accepts tool names
    # that match `^[a-zA-Z0-9_-]{1,64}$`, but the internal registry uses dotted
    # names such as `stock.search_catalogue`. We normalize at the protocol edge
    # so the public MCP contract stays valid while internal orchestration keeps
    # its existing stable identifiers.
    candidate = _NON_MCP_TOOL_NAME_CHARS.sub("_", name).strip("_-")
    if not candidate:
        candidate = "tool"
    if not candidate[0].isalnum():
        candidate = f"tool_{candidate}"
    if len(candidate) > 64:
        candidate = candidate[:64].rstrip("_-") or candidate[:64]
    if _MCP_TOOL_NAME_PATTERN.fullmatch(candidate):
        return candidate

    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    prefix = candidate[: max(1, 64 - len(digest) - 1)].rstrip("_-") or "tool"
    fallback = f"{prefix}_{digest}"
    return fallback[:64]


def is_mcp_safe_tool_name(name: str) -> bool:
    return bool(_MCP_TOOL_NAME_PATTERN.fullmatch(name))


class McpToolNameMap:
    def __init__(self, internal_names: list[str]) -> None:
        self.internal_to_public: dict[str, str] = {}
        self.public_to_internal: dict[str, str] = {}

        for internal_name in internal_names:
            public_name = normalize_mcp_tool_name(internal_name)
            if public_name in self.public_to_internal and self.public_to_internal[public_name] != internal_name:
                public_name = self._disambiguate(internal_name, public_name)
            self.internal_to_public[internal_name] = public_name
            self.public_to_internal[public_name] = internal_name

    def to_public(self, internal_name: str) -> str:
        return self.internal_to_public.get(internal_name, normalize_mcp_tool_name(internal_name))

    def to_internal(self, public_name: str) -> str:
        return self.public_to_internal.get(public_name, public_name)

    def _disambiguate(self, internal_name: str, public_name: str) -> str:
        digest = hashlib.sha1(internal_name.encode("utf-8")).hexdigest()[:8]
        prefix = public_name[: max(1, 64 - len(digest) - 1)].rstrip("_-") or "tool"
        candidate = f"{prefix}_{digest}"[:64]
        if candidate in self.public_to_internal and self.public_to_internal[candidate] != internal_name:
            digest = hashlib.sha1(f"{internal_name}:{public_name}".encode("utf-8")).hexdigest()[:8]
            prefix = prefix[: max(1, 64 - len(digest) - 1)].rstrip("_-") or "tool"
            candidate = f"{prefix}_{digest}"[:64]
        return candidate
