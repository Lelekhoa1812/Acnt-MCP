from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import ParameterMappingError, UnsupportedToolError
from app.mcp.output import build_envelope_output_schema
from app.mcp.tool import McpToolNameMap, is_mcp_safe_tool_name
from app.schemas import ToolDefinition, ToolResult, ToolTrace
from app.tool.accounting import (
    AccountingService,
    OpenCollectiveAccountSearchArgs,
    OpenCollectiveBudgetLookupArgs,
    OpenCollectiveCollectiveCreateArgs,
    OpenCollectiveCollectiveListArgs,
    OpenCollectiveCollectiveSearchArgs,
    OpenCollectiveCollectiveUpdateArgs,
    OpenCollectiveExpenseCreateArgs,
    OpenCollectiveExpenseDeleteArgs,
    OpenCollectiveExpenseListArgs,
    OpenCollectiveExpenseProcessArgs,
    OpenCollectiveExpenseUpdateArgs,
    OpenCollectiveExpenseWorkflowArgs,
    OpenCollectiveFinancialSnapshotArgs,
    OpenCollectiveHostListArgs,
    OpenCollectivePayeeCreateArgs,
    OpenCollectivePayeeListArgs,
    OpenCollectivePayeeViewArgs,
    OpenCollectiveTransactionAllArgs,
)
from app.tool.currency import (
    CurrencyConvertArgs,
    CurrencyFluctuationArgs,
    CurrencyHistoryArgs,
    CurrencyLatestArgs,
    CurrencyService,
    CurrencySymbolsArgs,
    CurrencyTimeseriesArgs,
)


@dataclass
class ToolSpec:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[BaseModel, str], Awaitable[ToolResult]]
    visible: bool = True
    output_model: type[BaseModel] | None = None


_TOOL_LOG_REDACT_KEYS: frozenset[str] = frozenset({"id"})


def _redact_args_for_tool_log(raw_args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw_args.items() if k not in _TOOL_LOG_REDACT_KEYS}


class ToolRegistry:
    def __init__(
        self,
        currency_service: CurrencyService,
        accounting_service: AccountingService,
        logger: logging.Logger,
        *,
        compact_envelope: bool = True,
    ) -> None:
        self.currency_service = currency_service
        self.accounting_service = accounting_service
        self.logger = logger
        self.compact_envelope = compact_envelope
        self._tools: dict[str, ToolSpec] = {}
        self._register_currency()
        self._register_accounting()
        self._tool_name_map = McpToolNameMap(list(self._tools))

    def list_tools(self, *, include_hidden: bool = True) -> list[ToolDefinition]:
        tools: list[ToolDefinition] = []
        for spec in self._tools.values():
            if not include_hidden and not spec.visible:
                continue
            public_name = self._tool_name_map.to_public(spec.name)
            if not is_mcp_safe_tool_name(public_name):  # pragma: no cover - defensive invariant
                raise UnsupportedToolError(
                    f"Configured tool '{spec.name}' produced unsafe MCP name '{public_name}'."
                )
            tools.append(
                ToolDefinition(
                    name=public_name,
                    description=spec.description,
                    input_schema=spec.model.model_json_schema(),
                    output_schema=self._build_output_schema(spec),
                )
            )
        return tools

    def tool_payloads(self, *, include_hidden: bool = True) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for spec in self._tools.values():
            if not include_hidden and not spec.visible:
                continue
            public_name = self._tool_name_map.to_public(spec.name)
            if not is_mcp_safe_tool_name(public_name):  # pragma: no cover - defensive invariant
                raise UnsupportedToolError(
                    f"Configured tool '{spec.name}' produced unsafe MCP name '{public_name}'."
                )
            payloads.append(
                {
                    "type": "function",
                    "function": {
                        "name": public_name,
                        "description": spec.description,
                        "parameters": spec.model.model_json_schema(),
                        "returns": self._build_output_schema(spec),
                    },
                }
            )
        return payloads

    async def call_tool(self, tool_name: str, raw_args: dict[str, Any]) -> ToolResult:
        tool_name = self.resolve_tool_name(tool_name)
        self.logger.debug("tool_call tool=%s args=%s", tool_name, _redact_args_for_tool_log(raw_args))
        spec = self._tools.get(tool_name)
        if spec is None:
            raise UnsupportedToolError(f"Unsupported tool '{tool_name}'.")
        try:
            validated = spec.model.model_validate(raw_args)
        except ValidationError as exc:
            raise ParameterMappingError(self._format_validation_error(tool_name, exc)) from exc
        start = time.perf_counter()
        result = await spec.handler(validated, "")
        duration_seconds = max(0.0, time.perf_counter() - start)
        if result.trace is not None:
            result.trace.duration_seconds = duration_seconds
        return result

    def resolve_tool_name(self, tool_name: str) -> str:
        if tool_name in self._tools:
            return tool_name
        return self._tool_name_map.to_internal(tool_name)

    def _format_validation_error(self, tool_name: str, exc: ValidationError) -> str:
        parts: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "Invalid value."))
            if location:
                parts.append(f"{location}: {message}")
            else:
                parts.append(message)
        rendered = "; ".join(parts) if parts else "Invalid arguments."
        return f"Invalid arguments for '{tool_name}': {rendered}"

    def _register(
        self,
        name: str,
        description: str,
        model: type[BaseModel],
        handler,
        *,
        visible: bool = True,
        output_model: type[BaseModel] | None = None,
    ) -> None:
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            model=model,
            handler=handler,
            visible=visible,
            output_model=output_model,
        )

    def _build_output_schema(self, spec: ToolSpec) -> dict[str, Any]:
        data_schema: dict[str, Any] | None = None
        if spec.output_model is not None:
            data_schema = spec.output_model.model_json_schema()
        return build_envelope_output_schema(data_schema, compact=self.compact_envelope)

    async def _convert_snapshot_for_display(
        self,
        data: dict[str, Any],
        display_currency: str,
    ) -> tuple[dict[str, Any], list[str]]:
        """Convert financial snapshot stat amounts to display_currency at today's FX rate."""
        account = data.get("account")
        if not isinstance(account, dict):
            return data, []
        stats = account.get("stats")
        if not isinstance(stats, dict):
            return data, []
        balance = stats.get("balance")
        native_currency = balance.get("currency") if isinstance(balance, dict) else None
        if not isinstance(native_currency, str) or not native_currency:
            return data, []

        target = display_currency.upper()
        if native_currency.upper() == target:
            return data, [f"Amounts are already in {target} — no conversion applied."]

        try:
            rate_result, _, rate_notes = await self.currency_service.convert(
                CurrencyConvertArgs(from_code=native_currency, to=target, amount=1.0)
            )
            rate = (rate_result.get("info") or {}).get("rate") if isinstance(rate_result, dict) else None
            if not isinstance(rate, (int, float)) or rate <= 0:
                return data, [f"FX rate not available for {native_currency} → {target}."]
        except Exception as exc:
            return data, [f"FX conversion to {target} failed: {exc}"]

        def _apply_rate(amount_obj: object) -> dict[str, Any] | None:
            if not isinstance(amount_obj, dict):
                return None
            v = amount_obj.get("value")
            if not isinstance(v, (int, float)):
                return None
            cents = amount_obj.get("valueInCents")
            result: dict[str, Any] = {"value": round(v * rate, 2), "currency": target}
            if isinstance(cents, (int, float)):
                result["valueInCents"] = round(cents * rate)
            return result

        stat_keys = ("balance", "yearlyBudget", "monthlySpending", "totalAmountReceived", "totalAmountSpent")
        display_stats = {k: _apply_rate(stats.get(k)) for k in stat_keys}
        display_stats = {k: v for k, v in display_stats.items() if v is not None}

        summary = data.get("summary")
        display_summary: dict[str, Any] = {}
        if isinstance(summary, dict):
            total = summary.get("open_liability_amount_total")
            if isinstance(total, (int, float)):
                display_summary["open_liability_amount_total"] = round(total * rate, 2)
                display_summary["open_liability_currency"] = target

        converted: dict[str, Any] = {
            **data,
            "display_currency": target,
            "display_stats": display_stats,
        }
        if display_summary:
            converted["display_summary"] = display_summary

        notes = [f"Amounts converted from {native_currency} to {target} at rate {float(rate):.6f} (today's rate)."]
        notes.extend(rate_notes)
        return converted, notes

    def _register_currency(self) -> None:
        async def symbols(validated: CurrencySymbolsArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.symbols(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_symbols",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> symbols",
                result_count=len(data.get("symbols", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_symbols", data=data, normalization_notes=notes, trace=trace)

        async def latest(validated: CurrencyLatestArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.latest(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_latest",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> latest.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_latest", data=data, normalization_notes=notes, trace=trace)

        async def history(validated: CurrencyHistoryArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.history(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_history",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> historical.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_history", data=data, normalization_notes=notes, trace=trace)

        async def timeseries(validated: CurrencyTimeseriesArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.timeseries(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_series",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> timeseries.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_series", data=data, normalization_notes=notes, trace=trace)

        async def convert(validated: CurrencyConvertArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.convert(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_convert",
                args=validated.model_dump(by_alias=True, exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> conversion",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_convert", data=data, normalization_notes=notes, trace=trace)

        async def fluctuation(validated: CurrencyFluctuationArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.fluctuation(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_fluctuation",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> fluctuation.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_fluctuation", data=data, normalization_notes=notes, trace=trace)

        self._register(
            "fx_symbols",
            "Exchange Rates API supported currency codes. Use before FX lookups when the user gives unclear currency names.",
            CurrencySymbolsArgs,
            symbols,
        )
        self._register(
            "fx_latest",
            "Latest FX rates for an optional base and comma-separated target symbols.",
            CurrencyLatestArgs,
            latest,
        )
        self._register(
            "fx_history",
            "Historical FX rates for one YYYY-MM-DD date, optional base, and optional comma-separated target symbols.",
            CurrencyHistoryArgs,
            history,
        )
        self._register(
            "fx_series",
            "Daily FX rate series between start_date and end_date for optional base/target symbols.",
            CurrencyTimeseriesArgs,
            timeseries,
        )
        self._register(
            "fx_convert",
            "Convert a positive amount from one currency to another, optionally as of a YYYY-MM-DD date.",
            CurrencyConvertArgs,
            convert,
        )
        self._register(
            "fx_fluctuation",
            "FX rate fluctuation over a YYYY-MM-DD date range, comparing start and end rates.",
            CurrencyFluctuationArgs,
            fluctuation,
        )

    def _register_accounting(self) -> None:
        async def expense_list(validated: OpenCollectiveExpenseListArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.expense_list(validated)
            account = data.get("account", {}) if isinstance(data, dict) else {}
            expense_count = None
            if isinstance(account, dict):
                expenses = account.get("expenses")
                if isinstance(expenses, dict):
                    expense_count = expenses.get("totalCount")
            trace = ToolTrace(
                thought=thought,
                tool="accounting_expense_list",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL expenses query",
                result_count=expense_count if isinstance(expense_count, int) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_expense_list", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def transaction_all(validated: OpenCollectiveTransactionAllArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.transaction_all(validated)
            account = data.get("account", {}) if isinstance(data, dict) else {}
            transaction_count = None
            if isinstance(account, dict):
                transactions = account.get("transactions")
                if isinstance(transactions, dict):
                    transaction_count = transactions.get("totalCount")
            trace = ToolTrace(
                thought=thought,
                tool="accounting_transaction_all",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL transactions query",
                result_count=transaction_count if isinstance(transaction_count, int) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_transaction_all", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def collective_search(validated: OpenCollectiveCollectiveSearchArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.collective_search(validated)
            accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
            result_count = None
            if isinstance(accounts, dict):
                result_count = accounts.get("totalCount")
            trace = ToolTrace(
                thought=thought,
                tool="accounting_collective_search",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL accounts search query",
                result_count=result_count if isinstance(result_count, int) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_collective_search", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def financial_snapshot(validated: OpenCollectiveFinancialSnapshotArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.financial_snapshot(validated)
            if validated.display_currency and isinstance(data, dict):
                data, extra_notes = await self._convert_snapshot_for_display(data, validated.display_currency)
                notes = list(notes) + extra_notes
            summary = data.get("summary", {}) if isinstance(data, dict) else {}
            expense_count = summary.get("expense_count") if isinstance(summary, dict) else None
            transaction_count = summary.get("transaction_count") if isinstance(summary, dict) else None
            result_count = expense_count if isinstance(expense_count, int) else None
            if result_count is None and isinstance(transaction_count, int):
                result_count = transaction_count
            trace = ToolTrace(
                thought=thought,
                tool="accounting_financial_snapshot",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL account snapshot query",
                result_count=result_count,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_financial_snapshot", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def expense_workflow(validated: OpenCollectiveExpenseWorkflowArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.expense_workflow(validated)
            operation = data.get("expense", {}) if isinstance(data, dict) else {}
            trace = ToolTrace(
                thought=thought,
                tool="accounting_expense_workflow",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL createExpense/editExpense/deleteExpense/processExpense mutations",
                result_count=1 if isinstance(operation, dict) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_expense_workflow", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def budget_lookup(validated: OpenCollectiveBudgetLookupArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.budget_lookup(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_budget_lookup",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL account stats query",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_budget_lookup", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def expense_create(validated: OpenCollectiveExpenseCreateArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.create_expense(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_expense_create",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL createExpense mutation",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_expense_create", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def expense_update(validated: OpenCollectiveExpenseUpdateArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.edit_expense(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_expense_update",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL editExpense mutation",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_expense_update", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def expense_delete(validated: OpenCollectiveExpenseDeleteArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.delete_expense(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_expense_delete",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL deleteExpense mutation",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_expense_delete", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def expense_process(validated: OpenCollectiveExpenseProcessArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.process_expense(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_expense_process",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL processExpense mutation",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_expense_process", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        # Root Cause vs Logic: alias tooling historically named accounting_account_search; reuse collective_search handler to keep behavior consistent without duplicating logic.
        self._register(
            "accounting_account_search",
            "Open Collective client search for resolving human labels, ambiguous client names, closest-match candidates, and create-or-confirm decisions before ledger actions.",
            OpenCollectiveAccountSearchArgs,
            collective_search,
            visible=False,
        )
        self._register(
            "accounting_collective_search",
            "Open Collective collective search for resolving human labels, ambiguous collective names, closest-match suggestions, and create-confirmation decisions before ledger actions. If not found, follow with accounting_collective_list to browse or accounting_collective_create to create.",
            OpenCollectiveCollectiveSearchArgs,
            collective_search,
        )
        self._register(
            "accounting_financial_snapshot",
            "Open Collective financial snapshot for a reconciled view of client balance, paid-to-date, recent expenses, bank transactions, and open liabilities in one call. Pass display_currency (e.g. 'AUD') to also receive all stat amounts converted to that currency at today's FX rate alongside the collective's native amounts.",
            OpenCollectiveFinancialSnapshotArgs,
            financial_snapshot,
        )
        self._register(
            "accounting_expense_workflow",
            "Open Collective expense workflow for creating, editing, deleting, and processing expenses (archive/restore/mark-as-paid/invoice) from one structured action tool. For CREATE: first confirm the collective with accounting_collective_search or accounting_collective_list, then confirm the payee with accounting_payee_list; create missing entities with accounting_collective_create or accounting_payee_create before filing.",
            OpenCollectiveExpenseWorkflowArgs,
            expense_workflow,
        )
        async def host_list(validated: OpenCollectiveHostListArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.host_list(validated)
            hosts = data.get("hosts", {}) if isinstance(data, dict) else {}
            result_count = hosts.get("totalCount") if isinstance(hosts, dict) else None
            trace = ToolTrace(
                thought=thought,
                tool="accounting_host_list",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL hosts query",
                result_count=result_count if isinstance(result_count, int) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_host_list", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def collective_list(validated: OpenCollectiveCollectiveListArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.collective_list(validated)
            accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
            result_count = accounts.get("totalCount") if isinstance(accounts, dict) else None
            trace = ToolTrace(
                thought=thought,
                tool="accounting_collective_list",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL collective list query",
                result_count=result_count if isinstance(result_count, int) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_collective_list", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def collective_create(validated: OpenCollectiveCollectiveCreateArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.collective_create(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_collective_create",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL createCollective mutation",
                result_count=1 if isinstance(data.get("collective"), dict) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_collective_create", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def collective_update(validated: OpenCollectiveCollectiveUpdateArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.collective_update(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_collective_update",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL editCollective mutation",
                result_count=1 if isinstance(data.get("collective"), dict) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_collective_update", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def payee_list(validated: OpenCollectivePayeeListArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.payee_list(validated)
            accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
            result_count = accounts.get("totalCount") if isinstance(accounts, dict) else None
            trace = ToolTrace(
                thought=thought,
                tool="accounting_payee_list",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL accounts query (INDIVIDUAL/ORGANIZATION/VENDOR)",
                result_count=result_count if isinstance(result_count, int) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_payee_list", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def payee_view(validated: OpenCollectivePayeeViewArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.payee_view(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_payee_view",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL account query",
                result_count=1 if isinstance(data.get("account"), dict) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_payee_view", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def payee_create(validated: OpenCollectivePayeeCreateArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.payee_create(validated)
            trace = ToolTrace(
                thought=thought,
                tool="accounting_payee_create",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL createOrganization mutation",
                result_count=1 if isinstance(data.get("organization"), dict) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_payee_create", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        self._register(
            "accounting_collective_list",
            "List Open Collective collectives accessible to the authenticated user. Call this first when the user wants to manage expenses but hasn't confirmed which collective to use — list and confirm before proceeding.",
            OpenCollectiveCollectiveListArgs,
            collective_list,
        )
        self._register(
            "accounting_host_list",
            "List available Open Collective fiscal hosts. Call this before accounting_collective_create to discover valid host slugs — pass the chosen slug as host.slug when creating a collective.",
            OpenCollectiveHostListArgs,
            host_list,
        )
        self._register(
            "accounting_collective_create",
            "Create a new Open Collective collective under a fiscal host. Call accounting_host_list first to find a valid host slug, then call this after accounting_collective_search confirms the collective does not exist and the user approves creation.",
            OpenCollectiveCollectiveCreateArgs,
            collective_create,
        )
        self._register(
            "accounting_collective_update",
            "Update an existing Open Collective collective's settings — including native currency, name, and description. Use accounting_collective_search first to confirm the slug/id, then call this with the fields to change. Changing currency updates the collective's default accounting currency going forward.",
            OpenCollectiveCollectiveUpdateArgs,
            collective_update,
        )
        self._register(
            "accounting_payee_list",
            "List payee accounts (INDIVIDUAL, ORGANIZATION, VENDOR) on Open Collective. Use before creating an expense to confirm the payee exists; offer accounting_payee_create if the payee is absent.",
            OpenCollectivePayeeListArgs,
            payee_list,
        )
        self._register(
            "accounting_payee_view",
            "Retrieve the full profile, legal name, and saved payout methods for a single payee by slug.",
            OpenCollectivePayeeViewArgs,
            payee_view,
        )
        self._register(
            "accounting_payee_create",
            "Create a new vendor payee under a fiscal host. Call after accounting_payee_list confirms the payee is absent and the user approves creation.",
            OpenCollectivePayeeCreateArgs,
            payee_create,
        )
        self._register(
            "accounting_expense_list",
            "Open Collective expense list for read-only expense reports and ledger entries scoped to a client.",
            OpenCollectiveExpenseListArgs,
            expense_list,
            visible=False,
        )
        self._register(
            "accounting_transaction_all",
            "Open Collective bank-transaction (or payments fallback) lookup for transaction histories and audit-style browsing.",
            OpenCollectiveTransactionAllArgs,
            transaction_all,
            visible=False,
        )
        self._register(
            "accounting_expense_create",
            "Open Collective expense creation for logging new invoices or receipts against a client.",
            OpenCollectiveExpenseCreateArgs,
            expense_create,
            visible=False,
        )
        self._register(
            "accounting_expense_update",
            "Open Collective expense editing for correcting or annotating existing ledger entries.",
            OpenCollectiveExpenseUpdateArgs,
            expense_update,
            visible=False,
        )
        self._register(
            "accounting_expense_delete",
            "Open Collective expense delete (soft-delete) for removing rejected expenses where allowed.",
            OpenCollectiveExpenseDeleteArgs,
            expense_delete,
            visible=False,
        )
        self._register(
            "accounting_expense_process",
            "Open Collective expense processing (bulk action) for archiving, restoring, marking-as-paid, or invoicing expenses.",
            OpenCollectiveExpenseProcessArgs,
            expense_process,
            visible=False,
        )
        self._register(
            "accounting_budget_lookup",
            "Open Collective client budget snapshot synthesised from balance, paid-to-date, and credit-balance fields.",
            OpenCollectiveBudgetLookupArgs,
            budget_lookup,
            visible=False,
        )
