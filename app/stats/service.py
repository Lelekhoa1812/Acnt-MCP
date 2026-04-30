from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from app.auth.models import UserContext
from app.config.settings import Settings
from app.schemas import ToolTrace
from app.store import AppKeyValueStore
from app.stats.models import UsageEvent, UsageStatsSnapshot, UsageUserGroup


class UsageStatsService:
    # Motivation vs Logic: admin review needs a compact, human-readable record
    # of recent user activity, so we append lightweight usage events once and
    # render them as grouped HTML instead of scraping logs or replaying traces.
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore) -> None:
        self.settings = settings
        self.key_value_store = key_value_store

    async def record_query(
        self,
        *,
        user_context: UserContext | None,
        query: str,
        tool_trace: list[ToolTrace],
    ) -> None:
        clean_query = " ".join(query.split())
        tool_names = self._extract_tool_names(tool_trace)
        if not clean_query and not tool_names:
            return
        await self._append_event(
            UsageEvent(
                recorded_at=time.time(),
                kind="query",
                user_oid=self._render_user_oid(user_context),
                user_email=self._render_user_email(user_context),
                query=clean_query or None,
                tool_names=tool_names,
            )
        )

    async def record_tool_call(self, *, user_context: UserContext | None, tool_name: str) -> None:
        clean_tool_name = tool_name.strip()
        if not clean_tool_name:
            return
        await self._append_event(
            UsageEvent(
                recorded_at=time.time(),
                kind="tool",
                user_oid=self._render_user_oid(user_context),
                user_email=self._render_user_email(user_context),
                tool_names=[clean_tool_name],
            )
        )

    async def snapshot(self) -> UsageStatsSnapshot:
        raw, _ = await self.key_value_store.get_json("usage_stats", "events")
        events = self._load_events(raw)
        groups_by_key: "OrderedDict[tuple[str, str | None], UsageUserGroup]" = OrderedDict()
        for event in sorted(events, key=lambda item: item.recorded_at, reverse=True):
            group_key = self._group_key(event)
            group = groups_by_key.get(group_key)
            if group is None:
                group = UsageUserGroup(
                    identity_label=self._identity_label(event),
                    user_oid=event.user_oid,
                    user_email=event.user_email,
                )
                groups_by_key[group_key] = group
            group.events.append(event)

        return UsageStatsSnapshot(generated_at=time.time(), groups=list(groups_by_key.values()))

    async def _append_event(self, event: UsageEvent) -> None:
        raw, _ = await self.key_value_store.get_json("usage_stats", "events")
        events = self._load_events(raw)
        events.append(event)
        await self.key_value_store.set_json(
            namespace="usage_stats",
            key="events",
            value=[item.model_dump(mode="json") for item in events],
            ttl_seconds=None,
        )

    def _load_events(self, raw: Any) -> list[UsageEvent]:
        if not isinstance(raw, list):
            return []
        events: list[UsageEvent] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                events.append(UsageEvent.model_validate(item))
            except Exception:  # pragma: no cover - defensive data guard
                continue
        return events

    def _extract_tool_names(self, tool_trace: list[ToolTrace]) -> list[str]:
        seen: set[str] = set()
        tool_names: list[str] = []
        for trace in tool_trace:
            tool_name = str(getattr(trace, "tool", "")).strip()
            if not tool_name or tool_name in seen:
                continue
            seen.add(tool_name)
            tool_names.append(tool_name)
        return tool_names

    def _render_user_oid(self, user_context: UserContext | None) -> str | None:
        if user_context is None:
            return None
        rendered = user_context.oid.strip() if user_context.oid else ""
        return rendered or None

    def _render_user_email(self, user_context: UserContext | None) -> str | None:
        if user_context is None:
            return None
        rendered = user_context.email.strip() if user_context.email else ""
        return rendered or None

    def _group_key(self, event: UsageEvent) -> tuple[str, str | None]:
        if event.user_oid:
            return ("oid", event.user_oid)
        if event.user_email:
            return ("email", event.user_email)
        return ("anonymous", None)

    def _identity_label(self, event: UsageEvent) -> str:
        if event.user_email and event.user_oid:
            return event.user_email
        if event.user_email:
            return event.user_email
        if event.user_oid:
            return event.user_oid
        return "Anonymous user"
