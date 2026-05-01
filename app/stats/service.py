from __future__ import annotations

import time
from collections import Counter, OrderedDict
from typing import Any

from app.auth.models import UserContext
from app.config.settings import Settings
from app.schemas import ToolTrace
from app.store import AppKeyValueStore
from app.stats.models import (
    ToolDurationRecord,
    UsageClientSummary,
    UsageEvent,
    UsageStatsSnapshot,
    UsageToolErrorSummary,
    UsageToolClientSummary,
    UsageToolSummary,
    UsageUserGroup,
)

TOOL_DURATION_HISTORY_LIMIT = 200

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
        client_id: str | None = None,
        client_name: str | None = None,
    ) -> None:
        clean_query = " ".join(query.split())
        tool_names = self._extract_tool_names(tool_trace)
        if not clean_query and not tool_names:
            return
        events = [
            UsageEvent(
                recorded_at=time.time(),
                kind="query",
                tenant_id=self._render_tenant_id(user_context),
                user_oid=self._render_user_oid(user_context),
                identity_key=self._render_identity_key(user_context),
                user_email=self._render_user_email(user_context),
                client_id=self._render_client_id(user_context, client_id),
                client_name=self._render_client_name(client_name),
                roles=self._render_roles(user_context),
                groups=self._render_groups(user_context),
                group_names=self._render_group_names(user_context),
                query=clean_query or None,
                tool_names=tool_names,
            )
        ]
        events.extend(
            self._tool_error_events(
                user_context=user_context,
                query=clean_query or None,
                tool_trace=tool_trace,
                client_id=client_id,
                client_name=client_name,
            )
        )
        await self._append_events(events)
        if events:
            client_label = self._client_label(events[0])
            ai_key = self._ai_key(client_label)
            recorded = events[0].recorded_at
            for trace in tool_trace:
                duration = trace.duration_seconds
                if duration is None:
                    continue
                await self._record_tool_duration(
                    tool=trace.tool,
                    duration=duration,
                    client_label=client_label,
                    ai_key=ai_key,
                    recorded_at=recorded,
                )

    async def record_tool_call(
        self,
        *,
        user_context: UserContext | None,
        tool_name: str,
        client_id: str | None = None,
        client_name: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        clean_tool_name = tool_name.strip()
        if not clean_tool_name:
            return
        event = UsageEvent(
            recorded_at=time.time(),
            kind="tool",
            tenant_id=self._render_tenant_id(user_context),
            user_oid=self._render_user_oid(user_context),
            identity_key=self._render_identity_key(user_context),
            user_email=self._render_user_email(user_context),
            client_id=self._render_client_id(user_context, client_id),
            client_name=self._render_client_name(client_name),
            roles=self._render_roles(user_context),
            groups=self._render_groups(user_context),
            group_names=self._render_group_names(user_context),
            tool_names=[clean_tool_name],
        )
        await self._append_event(event)
        client_label = self._client_label(event)
        if duration_seconds is not None:
            await self._record_tool_duration(
                tool=clean_tool_name,
                duration=duration_seconds,
                client_label=client_label,
                ai_key=self._ai_key(client_label),
                recorded_at=event.recorded_at,
            )

    async def record_tool_error(
        self,
        *,
        user_context: UserContext | None,
        tool_name: str,
        error: Exception,
        tool_args: dict[str, Any] | None = None,
        client_id: str | None = None,
        client_name: str | None = None,
    ) -> None:
        clean_tool_name = tool_name.strip() or "<unknown>"
        error_message = str(error)
        await self._append_event(
            UsageEvent(
                recorded_at=time.time(),
                kind="tool_error",
                tenant_id=self._render_tenant_id(user_context),
                user_oid=self._render_user_oid(user_context),
                identity_key=self._render_identity_key(user_context),
                user_email=self._render_user_email(user_context),
                client_id=self._render_client_id(user_context, client_id),
                client_name=self._render_client_name(client_name),
                roles=self._render_roles(user_context),
                groups=self._render_groups(user_context),
                group_names=self._render_group_names(user_context),
                query=self._render_tool_request(clean_tool_name, tool_args or {}),
                tool_names=[clean_tool_name],
                error_status_code=getattr(error, "status_code", None),
                error_message=error_message,
                error_request=getattr(error, "request", None) or self._render_tool_request(clean_tool_name, tool_args or {}),
            )
        )

    async def snapshot(self) -> UsageStatsSnapshot:
        raw, _ = await self.key_value_store.get_json("usage_stats", "events")
        events = self._load_events(raw)
        groups_by_key: "OrderedDict[tuple[str, str | None], UsageUserGroup]" = OrderedDict()
        configured_groups = self._configured_access_groups()
        for event in sorted(events, key=lambda item: item.recorded_at, reverse=True):
            if event.kind == "tool_error":
                continue
            group_key = self._group_key(event)
            group = groups_by_key.get(group_key)
            matched_groups = self._matching_configured_groups(event, configured_groups)
            if group is None:
                group = UsageUserGroup(
                    identity_label=self._identity_label(event),
                    tenant_id=event.tenant_id,
                    user_oid=event.user_oid,
                    identity_key=event.identity_key,
                    user_email=event.user_email,
                    client_id=event.client_id,
                    client_name=event.client_name,
                    roles=event.roles,
                    groups=event.groups,
                    group_names=event.group_names,
                    matched_groups=matched_groups,
                )
                groups_by_key[group_key] = group
            else:
                group.roles = self._merge_values(group.roles, event.roles)
                group.groups = self._merge_values(group.groups, event.groups)
                group.group_names = self._merge_values(group.group_names, event.group_names)
                group.matched_groups = self._merge_values(group.matched_groups, matched_groups)
            group.events.append(event)

        for group in groups_by_key.values():
            group.clients = self._summarize_clients(group.events)
            group.tools = self._summarize_tools(group.events)

        return UsageStatsSnapshot(
            generated_at=time.time(),
            groups=list(groups_by_key.values()),
            tool_errors=self._summarize_tool_errors(events),
        )

    async def _append_event(self, event: UsageEvent) -> None:
        await self._append_events([event])

    async def _append_events(self, new_events: list[UsageEvent]) -> None:
        raw, _ = await self.key_value_store.get_json("usage_stats", "events")
        events = self._load_events(raw)
        events.extend(new_events)
        await self.key_value_store.set_json(
            namespace="usage_stats",
            key="events",
            value=[item.model_dump(mode="json") for item in events],
            ttl_seconds=None,
        )

    async def tool_duration_snapshot(self, limit: int = 100) -> list[ToolDurationRecord]:
        raw, _ = await self.key_value_store.get_json("usage_stats", "tool_durations")
        records = self._load_duration_records(raw)
        if limit <= 0:
            return records
        return records[-limit:]

    async def _append_duration_record(self, record: ToolDurationRecord) -> None:
        raw, _ = await self.key_value_store.get_json("usage_stats", "tool_durations")
        records = self._load_duration_records(raw)
        records.append(record)
        if len(records) > TOOL_DURATION_HISTORY_LIMIT:
            records = records[-TOOL_DURATION_HISTORY_LIMIT:]
        await self.key_value_store.set_json(
            namespace="usage_stats",
            key="tool_durations",
            value=[item.model_dump(mode="json") for item in records],
            ttl_seconds=None,
        )

    def _load_duration_records(self, raw: Any) -> list[ToolDurationRecord]:
        if not isinstance(raw, list):
            return []
        records: list[ToolDurationRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                records.append(ToolDurationRecord.model_validate(item))
            except Exception:
                continue
        return records

    async def _record_tool_duration(
        self,
        *,
        tool: str,
        duration: float,
        client_label: str | None,
        ai_key: str,
        recorded_at: float | None = None,
    ) -> None:
        if duration < 0:
            return
        record = ToolDurationRecord(
            recorded_at=recorded_at or time.time(),
            tool=tool,
            duration_seconds=duration,
            client_label=client_label,
            ai_key=ai_key or "other",
        )
        await self._append_duration_record(record)

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
        tool_names: list[str] = []
        for trace in tool_trace:
            tool_name = str(getattr(trace, "tool", "")).strip()
            if not tool_name:
                continue
            tool_names.append(tool_name)
        return tool_names

    def _tool_error_events(
        self,
        *,
        user_context: UserContext | None,
        query: str | None,
        tool_trace: list[ToolTrace],
        client_id: str | None,
        client_name: str | None,
    ) -> list[UsageEvent]:
        events: list[UsageEvent] = []
        for trace in tool_trace:
            if trace.status != "error":
                continue
            tool_name = str(getattr(trace, "tool", "")).strip() or "<unknown>"
            message = "; ".join(trace.normalization_notes) or "Tool call failed."
            events.append(
                UsageEvent(
                    recorded_at=time.time(),
                    kind="tool_error",
                    tenant_id=self._render_tenant_id(user_context),
                    user_oid=self._render_user_oid(user_context),
                    identity_key=self._render_identity_key(user_context),
                    user_email=self._render_user_email(user_context),
                    client_id=self._render_client_id(user_context, client_id),
                    client_name=self._render_client_name(client_name),
                    roles=self._render_roles(user_context),
                    groups=self._render_groups(user_context),
                    group_names=self._render_group_names(user_context),
                    query=query,
                    tool_names=[tool_name],
                    error_status_code=trace.error_status_code,
                    error_message=message,
                    error_request=trace.error_request or self._render_tool_request(tool_name, trace.args),
                )
            )
        return events

    def _render_tenant_id(self, user_context: UserContext | None) -> str | None:
        if user_context is None:
            return None
        rendered = user_context.tenant_id.strip() if user_context.tenant_id else ""
        return rendered or None

    def _render_user_oid(self, user_context: UserContext | None) -> str | None:
        if user_context is None:
            return None
        rendered = user_context.oid.strip() if user_context.oid else ""
        return rendered or None

    def _render_identity_key(self, user_context: UserContext | None) -> str | None:
        tenant_id = self._render_tenant_id(user_context)
        user_oid = self._render_user_oid(user_context)
        if tenant_id and user_oid:
            return f"{tenant_id}:{user_oid}"
        return user_oid

    def _render_user_email(self, user_context: UserContext | None) -> str | None:
        if user_context is None:
            return None
        rendered = user_context.email.strip() if user_context.email else ""
        return rendered or None

    def _render_client_id(self, user_context: UserContext | None, client_id: str | None) -> str | None:
        rendered = (client_id or "").strip()
        if rendered:
            return rendered
        if user_context is None:
            return None
        rendered = user_context.client_id.strip() if user_context.client_id else ""
        return rendered or None

    def _render_client_name(self, client_name: str | None) -> str | None:
        rendered = (client_name or "").strip()
        return rendered or None

    def _render_roles(self, user_context: UserContext | None) -> list[str]:
        return list(user_context.roles) if user_context is not None else []

    def _render_groups(self, user_context: UserContext | None) -> list[str]:
        return list(user_context.groups) if user_context is not None else []

    def _render_group_names(self, user_context: UserContext | None) -> list[str]:
        return list(user_context.group_names) if user_context is not None else []

    def _configured_access_groups(self) -> list[str]:
        # Motivation vs Logic: the stats page is for permission review, so it
        # should only surface groups operators configured as OAuth/plugin gates
        # instead of dumping every Entra group attached to the user token.
        values: list[str] = []
        for configured in (
            self.settings.parsed_oauth_user_groups
            + self.settings.parsed_news_plugin_groups
            + self.settings.parsed_weather_plugin_groups
            + self.settings.parsed_currency_plugin_groups
            + self.settings.parsed_stock_plugin_groups
        ):
            cleaned = configured.strip()
            if not cleaned or cleaned.casefold() == "all":
                continue
            if cleaned.casefold() not in {value.casefold() for value in values}:
                values.append(cleaned)
        return values

    def _matching_configured_groups(self, event: UsageEvent, configured_groups: list[str]) -> list[str]:
        user_group_ids = self._normalized_set(event.groups)
        user_group_names = self._normalized_set(event.group_names)
        matches: list[str] = []
        for configured_group in configured_groups:
            normalized = configured_group.casefold()
            if normalized in user_group_names or normalized in user_group_ids:
                matches.append(self._display_group_label(configured_group, event))
        return self._merge_values([], matches)

    @staticmethod
    def _display_group_label(configured_group: str, event: UsageEvent) -> str:
        normalized = configured_group.casefold()
        for group_name in event.group_names:
            if group_name.strip().casefold() == normalized:
                return group_name.strip()
        for index, group_id in enumerate(event.groups):
            if group_id.strip().casefold() == normalized:
                if index < len(event.group_names) and event.group_names[index].strip():
                    return event.group_names[index].strip()
                return configured_group
        return configured_group

    def _summarize_clients(self, events: list[UsageEvent]) -> list[UsageClientSummary]:
        client_counts: Counter[str] = Counter()
        for event in events:
            client_counts[self._client_label(event)] += 1
        return [
            UsageClientSummary(label=label, ai_key=self._ai_key(label), count=count)
            for label, count in client_counts.most_common()
        ]

    def _summarize_tools(self, events: list[UsageEvent]) -> list[UsageToolSummary]:
        tool_counts: Counter[str] = Counter()
        tool_client_counts: dict[str, Counter[str]] = {}
        for event in events:
            client_label = self._client_label(event)
            for tool_name in event.tool_names:
                cleaned = tool_name.strip()
                if not cleaned:
                    continue
                tool_counts[cleaned] += 1
                tool_client_counts.setdefault(cleaned, Counter())[client_label] += 1

        summaries: list[UsageToolSummary] = []
        for tool_name, count in tool_counts.most_common():
            client_summaries = [
                UsageToolClientSummary(label=label, ai_key=self._ai_key(label), count=client_count)
                for label, client_count in tool_client_counts.get(tool_name, Counter()).most_common()
            ]
            summaries.append(UsageToolSummary(name=tool_name, count=count, clients=client_summaries))
        return summaries

    def _summarize_tool_errors(self, events: list[UsageEvent]) -> list[UsageToolErrorSummary]:
        summaries: list[UsageToolErrorSummary] = []
        for event in sorted(events, key=lambda item: item.recorded_at, reverse=True):
            if event.kind != "tool_error":
                continue
            client_label = self._client_label(event)
            summaries.append(
                UsageToolErrorSummary(
                    recorded_at=event.recorded_at,
                    identity_label=self._identity_label(event),
                    user_email=event.user_email,
                    client_label=client_label,
                    ai_key=self._ai_key(client_label),
                    tool_name=event.tool_names[0] if event.tool_names else "<unknown>",
                    query=event.query,
                    error_request=event.error_request,
                    error_status_code=event.error_status_code,
                    error_message=event.error_message,
                )
            )
        return summaries[:25]

    @staticmethod
    def _client_label(event: UsageEvent) -> str:
        if event.client_name and event.client_id:
            return f"{event.client_name} ({event.client_id})"
        if event.client_name:
            return event.client_name
        if event.client_id:
            return event.client_id
        return "Unknown AI"

    @staticmethod
    def _render_tool_request(tool_name: str, tool_args: dict[str, Any]) -> str:
        compact_args = {
            key: value
            for key, value in tool_args.items()
            if value is not None and value != "" and key not in {"thought"}
        }
        return f"{tool_name} {compact_args}" if compact_args else tool_name

    @staticmethod
    def _ai_key(label: str) -> str:
        normalized = label.casefold()
        if "claude" in normalized or "anthropic" in normalized:
            return "claude"
        if "cursor" in normalized:
            return "cursor"
        if "codex" in normalized:
            return "codex"
        if "chatgpt" in normalized:
            return "chatgpt"
        if "openai" in normalized:
            return "openai"
        return "other"

    @staticmethod
    def _merge_values(existing: list[str], incoming: list[str]) -> list[str]:
        merged = list(existing)
        seen = {value.casefold() for value in merged}
        for value in incoming:
            cleaned = value.strip()
            if not cleaned or cleaned.casefold() in seen:
                continue
            merged.append(cleaned)
            seen.add(cleaned.casefold())
        return merged

    @staticmethod
    def _normalized_set(values: list[str]) -> set[str]:
        return {value.strip().casefold() for value in values if value.strip()}

    def _group_key(self, event: UsageEvent) -> tuple[str, str | None]:
        if event.tenant_id and event.user_oid:
            return ("tenant_oid", f"{event.tenant_id}:{event.user_oid}")
        if event.identity_key:
            return ("identity", event.identity_key)
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
