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
    OpenCollectiveExpenseCreateArgs,
    OpenCollectiveExpenseDeleteArgs,
    OpenCollectiveExpenseListArgs,
    OpenCollectiveExpenseProcessArgs,
    OpenCollectiveExpenseUpdateArgs,
    OpenCollectiveExpenseWorkflowArgs,
    OpenCollectiveFinancialSnapshotArgs,
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

        async def account_search(validated: OpenCollectiveAccountSearchArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.account_search(validated)
            accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
            result_count = None
            if isinstance(accounts, dict):
                result_count = accounts.get("totalCount")
            trace = ToolTrace(
                thought=thought,
                tool="accounting_account_search",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="opencollective -> GraphQL accounts search query",
                result_count=result_count if isinstance(result_count, int) else None,
                normalization_notes=notes,
            )
            return ToolResult(tool="accounting_account_search", data=data, llm_content=data, normalization_notes=notes, trace=trace)

        async def collective_search(validated: OpenCollectiveAccountSearchArgs, thought: str) -> ToolResult:
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
                source_data="opencollective -> GraphQL accounts list query (type-filtered)",
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
                source_data="opencollective -> GraphQL accounts list query (USER/ORGANIZATION types)",
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
                source_data="opencollective -> GraphQL account detail query",
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

        async def financial_snapshot(validated: OpenCollectiveFinancialSnapshotArgs, thought: str) -> ToolResult:
            data, cache_status, notes = await self.accounting_service.financial_snapshot(validated)
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

        self._register(
            "accounting_collective_search",
            "Open Collective collective search — resolves human labels, ambiguous names, and closest-match suggestions before ledger actions. Returns resolution.status: recommended | ambiguous | not_found.",
            OpenCollectiveAccountSearchArgs,
            collective_search,
        )
        self._register(
            "accounting_collective_list",
            "Open Collective collective browser — lists all accessible collectives with optional type filter (COLLECTIVE, FUND, ORGANIZATION, etc.) and keyword search.",
            OpenCollectiveCollectiveListArgs,
            collective_list,
        )
        self._register(
            "accounting_collective_create",
            "Open Collective collective creation — creates a new collective under a specified host. Use when accounting_collective_search returns not_found and the user wants to create a new one.",
            OpenCollectiveCollectiveCreateArgs,
            collective_create,
        )
        self._register(
            "accounting_payee_list",
            "Open Collective payee browser — lists USER and ORGANIZATION accounts that can receive expense payments, with optional keyword search.",
            OpenCollectivePayeeListArgs,
            payee_list,
        )
        self._register(
            "accounting_payee_view",
            "Open Collective payee detail — fetches full account details (name, type, legal name, email, website) for a specific payee by slug or id.",
            OpenCollectivePayeeViewArgs,
            payee_view,
        )
        self._register(
            "accounting_payee_create",
            "Open Collective payee creation — creates a new organisation account that can be used as a payee in expense submissions.",
            OpenCollectivePayeeCreateArgs,
            payee_create,
        )
        self._register(
            "accounting_account_search",
            "Open Collective client search for resolving human labels, ambiguous client names, closest-match suggestions, and create-confirmation decisions before ledger actions.",
            OpenCollectiveAccountSearchArgs,
            account_search,
            visible=False,
        )
        self._register(
            "accounting_financial_snapshot",
            "Open Collective financial snapshot for a reconciled view of client balance, paid-to-date, recent expenses, bank transactions, and open liabilities in one call.",
            OpenCollectiveFinancialSnapshotArgs,
            financial_snapshot,
        )
        self._register(
            "accounting_expense_workflow",
            "Open Collective expense workflow for creating, editing, deleting, and processing expenses (archive/restore/mark-as-paid/invoice) from one structured action tool.",
            OpenCollectiveExpenseWorkflowArgs,
            expense_workflow,
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
