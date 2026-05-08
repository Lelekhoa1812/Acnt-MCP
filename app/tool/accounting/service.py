from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.tool.accounting.model import (
    OpenCollectiveBudgetLookupArgs,
    OpenCollectiveExpenseCreateArgs,
    OpenCollectiveExpenseDeleteArgs,
    OpenCollectiveExpenseListArgs,
    OpenCollectiveExpenseProcessArgs,
    OpenCollectiveExpenseUpdateArgs,
    OpenCollectiveTransactionAllArgs,
)
from app.config import Settings, UpstreamServiceError
from app.store import AppKeyValueStore


_CREATE_EXPENSE_MUTATION = """
mutation CreateExpense($account: AccountReferenceInput!, $expense: ExpenseCreateInput!, $privateComment: String) {
  createExpense(account: $account, expense: $expense, privateComment: $privateComment) {
    id
    legacyId
    slug
    status
    type
    description
    amount
    currency
    privateMessage
    reference
    payoutMethod {
      id
      type
      publicId
    }
    payee {
      id
      slug
      name
    }
    host {
      id
      slug
      name
    }
    items {
      id
      description
      incurredAt
      url
      amount {
        currency
        value
      }
    }
  }
}
""".strip()

_EDIT_EXPENSE_MUTATION = """
mutation EditExpense($expense: ExpenseUpdateInput!) {
  editExpense(expense: $expense) {
    id
    slug
    status
    description
    amount
    currency
    reference
    privateMessage
  }
}
""".strip()

_DELETE_EXPENSE_MUTATION = """
mutation DeleteExpense($expense: ExpenseReferenceInput!) {
  deleteExpense(expense: $expense) {
    id
    slug
    status
  }
}
""".strip()

_PROCESS_EXPENSE_MUTATION = """
mutation ProcessExpense(
  $expense: ExpenseReferenceInput!
  $action: ExpenseProcessAction!
  $message: String
  $paymentParams: ProcessExpensePaymentParams
) {
  processExpense(expense: $expense, action: $action, message: $message, paymentParams: $paymentParams) {
    id
    slug
    status
    type
    amount
    currency
    privateMessage
  }
}
""".strip()


class AccountingService:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger
        self._client = httpx.AsyncClient(base_url=self.settings.opencollective_graphql_url.rstrip("/"), timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def expense_list(self, args: OpenCollectiveExpenseListArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("opencollective_expense_list", args.model_dump(mode="json", exclude_none=True), lambda: self._expense_list_payload(args))

    async def transaction_all(self, args: OpenCollectiveTransactionAllArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("opencollective_transaction_all", args.model_dump(mode="json", exclude_none=True), lambda: self._transaction_all_payload(args))

    async def budget_lookup(self, args: OpenCollectiveBudgetLookupArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("opencollective_budget_lookup", args.model_dump(mode="json", exclude_none=True), lambda: self._budget_lookup_payload(args))

    async def _cached(
        self,
        namespace: str,
        payload: dict[str, object],
        loader,
    ) -> tuple[dict[str, object], str, list[str]]:
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"{namespace}:{json.dumps(payload, sort_keys=True, default=str)}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=loader,
        )
        return raw, cache_status, notes

    async def _expense_list_payload(self, args: OpenCollectiveExpenseListArgs) -> tuple[dict[str, object], list[str]]:
        query = """
query ExpenseList($slug: String!, $limit: Int!, $offset: Int!, $searchTerm: String) {
  account(slug: $slug) {
    id
    slug
    name
    expenses(limit: $limit, offset: $offset, searchTerm: $searchTerm) {
      totalCount
      nodes {
        id
        type
        status
        amount
        currency
        description
        createdAt
        account {
          id
          slug
          name
        }
      }
    }
  }
}
""".strip()
        payload = await self._post_graphql(
            query,
            {
                "slug": args.slug,
                "limit": args.limit,
                "offset": args.offset,
                "searchTerm": args.search_term,
            },
        )
        return self._shape_graphql_payload("expense_list", args, payload), []

    async def _transaction_all_payload(self, args: OpenCollectiveTransactionAllArgs) -> tuple[dict[str, object], list[str]]:
        query = """
query TransactionAll($slug: String!, $limit: Int!, $offset: Int!, $searchTerm: String) {
  account(slug: $slug) {
    id
    slug
    name
    transactions(limit: $limit, offset: $offset, searchTerm: $searchTerm) {
      totalCount
      nodes {
        id
        type
        kind
        amount
        currency
        description
        createdAt
        account {
          id
          slug
          name
        }
      }
    }
  }
}
""".strip()
        payload = await self._post_graphql(
            query,
            {
                "slug": args.slug,
                "limit": args.limit,
                "offset": args.offset,
                "searchTerm": args.search_term,
            },
        )
        return self._shape_graphql_payload("transaction_all", args, payload), []

    async def _budget_lookup_payload(self, args: OpenCollectiveBudgetLookupArgs) -> tuple[dict[str, object], list[str]]:
        query = """
query BudgetLookup($slug: String!) {
  account(slug: $slug) {
    id
    slug
    name
    stats {
      balance
      yearlyBudget
      monthlySpending
      totalAmountRaised
      totalAmountSpent
    }
  }
}
""".strip()
        payload = await self._post_graphql(query, {"slug": args.slug})
        return self._shape_graphql_payload("budget_lookup", args, payload), []

    async def create_expense(self, args: OpenCollectiveExpenseCreateArgs) -> tuple[dict[str, object], str, list[str]]:
        payload = await self._post_graphql(
            _CREATE_EXPENSE_MUTATION,
            {
                "account": args.account.model_dump(mode="json", exclude_none=True),
                "expense": args.expense.model_dump(mode="json", exclude_none=True),
                "privateComment": args.privateComment,
            },
        )
        return (
            self._shape_expense_operation_payload("opencollective_expense_create", args, payload, "createExpense"),
            "live",
            [],
        )

    async def edit_expense(self, args: OpenCollectiveExpenseUpdateArgs) -> tuple[dict[str, object], str, list[str]]:
        payload = await self._post_graphql(
            _EDIT_EXPENSE_MUTATION,
            {"expense": args.expense.model_dump(mode="json", exclude_none=True)},
        )
        return (
            self._shape_expense_operation_payload("opencollective_expense_update", args, payload, "editExpense"),
            "live",
            [],
        )

    async def delete_expense(self, args: OpenCollectiveExpenseDeleteArgs) -> tuple[dict[str, object], str, list[str]]:
        payload = await self._post_graphql(
            _DELETE_EXPENSE_MUTATION,
            {"expense": args.expense.model_dump(mode="json", exclude_none=True)},
        )
        return (
            self._shape_expense_operation_payload("opencollective_expense_delete", args, payload, "deleteExpense"),
            "live",
            [],
        )

    async def process_expense(self, args: OpenCollectiveExpenseProcessArgs) -> tuple[dict[str, object], str, list[str]]:
        payload = await self._post_graphql(
            _PROCESS_EXPENSE_MUTATION,
            {
                "expense": args.expense.model_dump(mode="json", exclude_none=True),
                "action": args.action.value,
                "message": args.message,
                "paymentParams": args.paymentParams.model_dump(mode="json", exclude_none=True) if args.paymentParams else None,
            },
        )
        return (
            self._shape_expense_operation_payload("opencollective_expense_process", args, payload, "processExpense"),
            "live",
            [],
        )

    async def _post_graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.opencollective_pat_token:
            headers["Personal-Token"] = self.settings.opencollective_pat_token
        response = await self._client.post("", json={"query": query, "variables": variables}, headers=headers)
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text, request="POST /graphql/v2")
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstreamServiceError(502, "Open Collective returned an unexpected non-object payload.", request="POST /graphql/v2")
        if payload.get("errors"):
            raise UpstreamServiceError(502, json.dumps(payload["errors"], ensure_ascii=False), request="POST /graphql/v2")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise UpstreamServiceError(502, "Open Collective returned a payload without data.", request="POST /graphql/v2")
        return data

    def _shape_graphql_payload(self, tool_name: str, args: Any, data: dict[str, Any]) -> dict[str, object]:
        account = data.get("account")
        if not isinstance(account, dict):
            return {
                "tool": tool_name,
                "query": args.model_dump(mode="json", exclude_none=True),
                "account": None,
                "raw": data,
            }
        return {
            "tool": tool_name,
            "query": args.model_dump(mode="json", exclude_none=True),
            "account": account,
            "raw": data,
        }

    # Motivation vs Logic: expose the same expense payload shape for all mutations so MCP tracing stays predictable.
    def _shape_expense_operation_payload(
        self,
        tool_name: str,
        args: Any,
        data: dict[str, Any],
        operation_key: str,
    ) -> dict[str, object]:
        return {
            "tool": tool_name,
            "query": args.model_dump(mode="json", exclude_none=True),
            "expense": data.get(operation_key),
            "raw": data,
        }
