from __future__ import annotations

import asyncio
import json
import logging
from difflib import SequenceMatcher
import re
from typing import Any

import httpx

from app.tool.accounting.model import (
    AccountReferenceInput,
    AmountInput,
    ExpenseCreateInput,
    ExpenseItemCreateInput,
    ExpenseProcessAction,
    ExpenseReferenceInput,
    ExpenseUpdateInput,
    ExpenseWorkflowAction,
    OpenCollectiveAccountSearchArgs,
    OpenCollectiveBudgetLookupArgs,
    OpenCollectiveCollectiveCreateArgs,
    OpenCollectiveCollectiveListArgs,
    OpenCollectiveCollectiveSearchArgs,
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
    OrganizationCreateInput,
    PayoutMethodInput,
    ProcessExpensePaymentParams,
)
from app.config import Settings, UpstreamServiceError
from app.store import AppKeyValueStore


_CREATE_EXPENSE_MUTATION = """
fragment OCAmountFields on Amount {
  value
  currency
  valueInCents
}

mutation CreateExpense($account: AccountReferenceInput!, $expense: ExpenseCreateInput!, $privateComment: String) {
  createExpense(account: $account, expense: $expense, privateComment: $privateComment) {
    id
    legacyId
    status
    type
    description
    amountV2 { ...OCAmountFields }
    currency
    privateMessage
    reference
    payoutMethod {
      id
      type
    }
    payee {
      id
      slug
      name
    }
    account {
      id
      slug
      name
    }
    items {
      id
      description
      incurredAt
      url
      amountV2 { ...OCAmountFields }
    }
  }
}
""".strip()

_EDIT_EXPENSE_MUTATION = """
fragment OCAmountFields on Amount {
  value
  currency
  valueInCents
}

mutation EditExpense($expense: ExpenseUpdateInput!) {
  editExpense(expense: $expense) {
    id
    legacyId
    status
    description
    amountV2 { ...OCAmountFields }
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
    legacyId
    status
  }
}
""".strip()

_PROCESS_EXPENSE_MUTATION = """
fragment OCAmountFields on Amount {
  value
  currency
  valueInCents
}

mutation ProcessExpense(
  $expense: ExpenseReferenceInput!
  $action: ExpenseProcessAction!
  $message: String
  $paymentParams: ProcessExpensePaymentParams
) {
  processExpense(expense: $expense, action: $action, message: $message, paymentParams: $paymentParams) {
    id
    legacyId
    status
    type
    amountV2 { ...OCAmountFields }
    currency
    privateMessage
  }
}
""".strip()

_ACCOUNT_SEARCH_QUERY = """
query AccountSearch($searchTerm: String!, $limit: Int!, $offset: Int!, $includeArchived: Boolean) {
  accounts(limit: $limit, offset: $offset, searchTerm: $searchTerm, includeArchived: $includeArchived, tagSearchOperator: AND) {
    totalCount
    nodes {
      id
      slug
      name
      type
    }
  }
}
""".strip()

_FINANCIAL_SNAPSHOT_QUERY = """
fragment OCAmountFields on Amount {
  value
  currency
  valueInCents
}

query FinancialSnapshot(
  $slug: String!
  $expenseLimit: Int!
  $expenseOffset: Int!
  $transactionLimit: Int!
  $transactionOffset: Int!
  $expenseSearchTerm: String
  $transactionSearchTerm: String
) {
  account(slug: $slug, throwIfMissing: false) {
    id
    slug
    name
    type
    stats {
      balance { ...OCAmountFields }
      yearlyBudget { ...OCAmountFields }
      monthlySpending { ...OCAmountFields }
      totalAmountReceived { ...OCAmountFields }
      totalAmountSpent { ...OCAmountFields }
    }
    expenses(limit: $expenseLimit, offset: $expenseOffset, searchTerm: $expenseSearchTerm) {
      totalCount
      nodes {
        id
        type
        status
        amountV2 { ...OCAmountFields }
        description
        createdAt
        tags
        payee {
          id
          slug
          name
        }
        account {
          id
          slug
          name
        }
      }
    }
    transactions(limit: $transactionLimit, offset: $transactionOffset, searchTerm: $transactionSearchTerm) {
      totalCount
      nodes {
        id
        type
        kind
        amount { ...OCAmountFields }
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


_COLLECTIVE_LIST_MEMBEROF_QUERY = """
query CollectiveList($limit: Int!, $offset: Int!, $roles: [MemberRole]) {
  loggedInAccount {
    id
    slug
    name
    memberOf(limit: $limit, offset: $offset, role: $roles, accountType: [COLLECTIVE, FUND, PROJECT, EVENT]) {
      totalCount
      nodes {
        account {
          id
          slug
          name
          type
          description
        }
        role
        since
      }
    }
  }
}
""".strip()

_COLLECTIVE_LIST_ACCOUNTS_QUERY = """
query CollectiveList($limit: Int!, $offset: Int!, $searchTerm: String, $includeArchived: Boolean, $type: [AccountType]) {
  accounts(limit: $limit, offset: $offset, searchTerm: $searchTerm, includeArchived: $includeArchived, type: $type, tagSearchOperator: AND) {
    totalCount
    nodes {
      id
      slug
      name
      type
      description
    }
  }
}
""".strip()

_CREATE_COLLECTIVE_MUTATION = """
mutation CreateCollective($collective: CollectiveCreateInput!, $host: AccountReferenceInput!, $message: String) {
  createCollective(collective: $collective, host: $host, message: $message) {
    id
    slug
    name
    type
    description
    createdAt
    host {
      id
      slug
      name
    }
  }
}
""".strip()

_PAYEE_LIST_QUERY = """
query PayeeList($limit: Int!, $offset: Int!, $searchTerm: String, $types: [AccountType]) {
  accounts(limit: $limit, offset: $offset, searchTerm: $searchTerm, type: $types, tagSearchOperator: AND) {
    totalCount
    nodes {
      id
      slug
      name
      type
      description
      legalName
      website
    }
  }
}
""".strip()

_PAYEE_VIEW_QUERY = """
query PayeeView($slug: String, $id: String) {
  account(slug: $slug, id: $id, throwIfMissing: false) {
    id
    slug
    name
    type
    description
    website
    ... on Organization {
      legalName
      email
    }
    ... on Individual {
      legalName
      email
    }
  }
}
""".strip()

_CREATE_ORGANIZATION_MUTATION = """
mutation CreateOrganization($organization: OrganizationCreateInput!) {
  createOrganization(organization: $organization) {
    id
    slug
    name
    type
    description
    website
    ... on Organization {
      legalName
      email
    }
  }
}
""".strip()


_HOSTS_LIST_QUERY = """
query HostList($limit: Int!, $offset: Int!, $searchTerm: String) {
  hosts(limit: $limit, offset: $offset, searchTerm: $searchTerm) {
    totalCount
    nodes {
      id
      slug
      name
      description
      type
    }
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
        return await self._cached("accounting_expense_list", args.model_dump(mode="json", exclude_none=True), lambda: self._expense_list_payload(args))

    async def transaction_all(self, args: OpenCollectiveTransactionAllArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("accounting_transaction_all", args.model_dump(mode="json", exclude_none=True), lambda: self._transaction_all_payload(args))

    async def budget_lookup(self, args: OpenCollectiveBudgetLookupArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("accounting_budget_lookup", args.model_dump(mode="json", exclude_none=True), lambda: self._budget_lookup_payload(args))

    async def collective_search(self, args: OpenCollectiveCollectiveSearchArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "accounting_collective_search",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._account_search_payload(args),
        )

    # backward-compat alias
    async def account_search(self, args: OpenCollectiveCollectiveSearchArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self.collective_search(args)

    async def financial_snapshot(self, args: OpenCollectiveFinancialSnapshotArgs) -> tuple[dict[str, object], str, list[str]]:
        # Exclude display_currency from the cache key — the OC data is the same regardless of
        # which display currency was requested; conversion is applied after the cache hit.
        cache_args = {k: v for k, v in args.model_dump(mode="json", exclude_none=True).items() if k != "display_currency"}
        return await self._cached(
            "accounting_financial_snapshot",
            cache_args,
            lambda: self._financial_snapshot_payload(args),
        )

    async def collective_list(self, args: OpenCollectiveCollectiveListArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "accounting_collective_list",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._collective_list_payload(args),
        )

    async def collective_create(self, args: OpenCollectiveCollectiveCreateArgs) -> tuple[dict[str, object], str, list[str]]:
        data, notes = await self._collective_create_payload(args)
        return data, "live", notes

    async def host_list(self, args: OpenCollectiveHostListArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "accounting_host_list",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._hosts_list_payload(args),
        )

    async def payee_list(self, args: OpenCollectivePayeeListArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "accounting_payee_list",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._payee_list_payload(args),
        )

    async def payee_view(self, args: OpenCollectivePayeeViewArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "accounting_payee_view",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._payee_view_payload(args),
        )

    async def payee_create(self, args: OpenCollectivePayeeCreateArgs) -> tuple[dict[str, object], str, list[str]]:
        data, notes = await self._payee_create_payload(args)
        return data, "live", notes

    async def expense_workflow(self, args: OpenCollectiveExpenseWorkflowArgs) -> tuple[dict[str, object], str, list[str]]:
        # Root Cause vs Logic: expense_workflow dispatches createExpense/editExpense/deleteExpense/processExpense
        # mutations - all writes - so the response must never be cached. A cached prior success would silently
        # short-circuit later calls with identical args, and a cached prior failure would mask transient upstream
        # issues until TTL expiry.
        data, notes = await self._expense_workflow_payload(args)
        return data, "live", notes

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

    async def _collective_list_payload(self, args: OpenCollectiveCollectiveListArgs) -> tuple[dict[str, object], list[str]]:
        use_member_of = bool(args.roles)
        if use_member_of:
            roles = args.roles or ["ADMIN", "MEMBER", "ACCOUNTANT"]
            payload = await self._post_graphql(
                _COLLECTIVE_LIST_MEMBEROF_QUERY,
                {"limit": args.limit, "offset": args.offset, "roles": roles},
            )
            logged_in = payload.get("loggedInAccount")
            accounts: dict[str, object] | None = None
            member_of = logged_in.get("memberOf") if isinstance(logged_in, dict) else None
            if isinstance(member_of, dict):
                nodes: list[dict[str, object]] = []
                for entry in member_of.get("nodes", []):
                    if not isinstance(entry, dict):
                        continue
                    account = entry.get("account")
                    if not isinstance(account, dict):
                        continue
                    node = dict(account)
                    role = entry.get("role")
                    since = entry.get("since")
                    if isinstance(role, str):
                        node["role"] = role
                    if isinstance(since, str):
                        node["since"] = since
                    nodes.append(node)
                accounts = {"totalCount": member_of.get("totalCount"), "nodes": nodes}
        else:
            payload = await self._post_graphql(
                _COLLECTIVE_LIST_ACCOUNTS_QUERY,
                {
                    "limit": args.limit,
                    "offset": args.offset,
                    "searchTerm": args.search_term,
                    "includeArchived": args.include_archived,
                    "type": args.type,
                },
            )
            accounts = payload.get("accounts") if isinstance(payload.get("accounts"), dict) else None
            logged_in = None
        return {
            "tool": "accounting_collective_list",
            "query": args.model_dump(mode="json", exclude_none=True),
            "accounts": accounts,
            "loggedInAccount": logged_in if isinstance(logged_in, dict) else None,
            "raw": payload,
        }, []

    async def _collective_create_payload(self, args: OpenCollectiveCollectiveCreateArgs) -> tuple[dict[str, object], list[str]]:
        variables: dict[str, Any] = {
            "collective": args.collective.model_dump(mode="json", exclude_none=True),
            "host": args.host.model_dump(mode="json", exclude_none=True),
            "message": args.message,
        }
        payload = await self._post_graphql(_CREATE_COLLECTIVE_MUTATION, variables)
        collective = payload.get("createCollective")
        notes: list[str] = []
        if isinstance(collective, dict):
            notes.append(
                f"Collective '{collective.get('slug')}' created successfully. "
                "You can now file expenses against it using accounting_expense_workflow."
            )
        return {
            "tool": "accounting_collective_create",
            "query": args.model_dump(mode="json", exclude_none=True),
            "collective": collective if isinstance(collective, dict) else None,
            "raw": payload,
        }, notes

    async def _hosts_list_payload(self, args: OpenCollectiveHostListArgs) -> tuple[dict[str, object], list[str]]:
        payload = await self._post_graphql(
            _HOSTS_LIST_QUERY,
            {
                "limit": args.limit,
                "offset": args.offset,
                "searchTerm": args.search_term,
            },
        )
        hosts = payload.get("hosts")
        notes: list[str] = []
        if isinstance(hosts, dict):
            total = hosts.get("totalCount", 0)
            nodes = hosts.get("nodes") or []
            if total == 0:
                notes.append("No fiscal hosts found. Contact Open Collective support or use a known public host such as 'opensource'.")
            else:
                slugs = [n.get("slug") for n in nodes if isinstance(n, dict) and n.get("slug")]
                notes.append(
                    f"Found {total} fiscal host(s). Pass one of these slugs as host.slug in accounting_collective_create: {', '.join(slugs)}."
                )
        return {
            "tool": "accounting_host_list",
            "query": args.model_dump(mode="json", exclude_none=True),
            "hosts": hosts if isinstance(hosts, dict) else None,
            "raw": payload,
        }, notes

    async def _payee_list_payload(self, args: OpenCollectivePayeeListArgs) -> tuple[dict[str, object], list[str]]:
        types = args.types or ["INDIVIDUAL", "ORGANIZATION", "VENDOR"]
        payload = await self._post_graphql(
            _PAYEE_LIST_QUERY,
            {
                "limit": args.limit,
                "offset": args.offset,
                "searchTerm": args.search_term,
                "types": types,
            },
        )
        accounts = payload.get("accounts")
        notes: list[str] = []
        if isinstance(accounts, dict):
            total = accounts.get("totalCount", 0)
            if total == 0:
                search_hint = f" matching '{args.search_term}'" if args.search_term else ""
                notes.append(
                f"No payee accounts found{search_hint}. "
                "Use accounting_payee_create to add a new payee."
                )
        return {
            "tool": "accounting_payee_list",
            "query": args.model_dump(mode="json", exclude_none=True),
            "accounts": accounts if isinstance(accounts, dict) else None,
            "raw": payload,
        }, notes

    async def _payee_view_payload(self, args: OpenCollectivePayeeViewArgs) -> tuple[dict[str, object], list[str]]:
        variables: dict[str, object] = {}
        if args.slug:
            variables["slug"] = args.slug
        if args.id:
            variables["id"] = args.id
        payload = await self._post_graphql(_PAYEE_VIEW_QUERY, variables or {"slug": ""})
        account = payload.get("account")
        notes: list[str] = []
        if not isinstance(account, dict) or not account:
            notes.append(
                f"No payee account found for slug '{args.slug}'. "
                "Use accounting_payee_list to browse available payees or accounting_payee_create to add one."
            )
        return {
            "tool": "accounting_payee_view",
            "query": args.model_dump(mode="json", exclude_none=True),
            "account": account if isinstance(account, dict) else None,
            "raw": payload,
        }, notes

    async def _payee_create_payload(self, args: OpenCollectivePayeeCreateArgs) -> tuple[dict[str, object], list[str]]:
        payload = await self._post_graphql(
            _CREATE_ORGANIZATION_MUTATION,
            {"organization": args.organization.model_dump(mode="json", exclude_none=True)},
        )
        organization = payload.get("createOrganization")
        notes: list[str] = []
        if isinstance(organization, dict):
            notes.append(
                f"Organization payee '{organization.get('slug')}' created successfully. "
                "You can now reference it as a payee in accounting_expense_workflow."
            )
        return {
            "tool": "accounting_payee_create",
            "query": args.model_dump(mode="json", exclude_none=True),
            "organization": organization if isinstance(organization, dict) else None,
            "raw": payload,
        }, notes

    async def _expense_list_payload(self, args: OpenCollectiveExpenseListArgs) -> tuple[dict[str, object], list[str]]:
        query = """
query ExpenseList($slug: String!, $limit: Int!, $offset: Int!, $searchTerm: String) {
  account(slug: $slug, throwIfMissing: false) {
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
        payload, notes = await self._account_scoped_payload(
            query,
            {
                "slug": args.slug,
                "limit": args.limit,
                "offset": args.offset,
                "searchTerm": args.search_term,
            },
            args.slug,
        )
        return self._shape_graphql_payload("expense_list", args, payload), notes

    async def _transaction_all_payload(self, args: OpenCollectiveTransactionAllArgs) -> tuple[dict[str, object], list[str]]:
        query = """
fragment OCAmountFields on Amount {
  value
  currency
  valueInCents
}

query TransactionAll($slug: String!, $limit: Int!, $offset: Int!, $searchTerm: String) {
  account(slug: $slug, throwIfMissing: false) {
    id
    slug
    name
    transactions(limit: $limit, offset: $offset, searchTerm: $searchTerm) {
      totalCount
      nodes {
        id
        type
        kind
        amount { ...OCAmountFields }
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
        payload, notes = await self._account_scoped_payload(
            query,
            {
                "slug": args.slug,
                "limit": args.limit,
                "offset": args.offset,
                "searchTerm": args.search_term,
            },
            args.slug,
        )
        return self._shape_graphql_payload("transaction_all", args, payload), notes

    async def _budget_lookup_payload(self, args: OpenCollectiveBudgetLookupArgs) -> tuple[dict[str, object], list[str]]:
        # Root Cause vs Logic: OC GraphQL v2 exposes AccountStats money fields as Amount objects (scalar selection
        # fails validation); totalAmountRaised was renamed to totalAmountReceived on AccountStats. Unknown slugs can
        # also fail hard, so we resolve the label through accounts search before retrying the stats query.
        query = """
fragment OCAmountFields on Amount {
  value
  currency
  valueInCents
}

query BudgetLookup($slug: String!) {
  account(slug: $slug, throwIfMissing: false) {
    id
    slug
    name
    stats {
      balance {
        ...OCAmountFields
      }
      yearlyBudget {
        ...OCAmountFields
      }
      monthlySpending {
        ...OCAmountFields
      }
      totalAmountReceived {
        ...OCAmountFields
      }
      totalAmountSpent {
        ...OCAmountFields
      }
    }
  }
}
""".strip()
        payload, notes = await self._account_scoped_payload(query, {"slug": args.slug}, args.slug)
        return self._shape_graphql_payload("budget_lookup", args, payload), notes

    async def _account_search_payload(self, args: OpenCollectiveAccountSearchArgs) -> tuple[dict[str, object], list[str]]:
        # Motivation vs Logic: account search must do more than list matches; it should tell the agent when to confirm a
        # close match versus when to ask the user to create a new account/slug workflow.
        payload = await self._post_graphql(
            _ACCOUNT_SEARCH_QUERY,
            {
                "searchTerm": args.search_term,
                "limit": args.limit,
                "offset": args.offset,
                "includeArchived": args.include_archived,
            },
        )
        candidates = self._extract_account_candidates(payload)
        recommendation = self._recommend_account_candidate(args.search_term, candidates)
        if recommendation is not None:
            payload = {**payload, "resolution": {"status": "recommended", "recommended": recommendation}}
            notes = [
                f"Closest match for '{args.search_term}' is '{recommendation.get('slug')}'. Ask the user to confirm or create a new one if that is not the intended account."
            ]
        elif candidates:
            payload = {**payload, "resolution": {"status": "ambiguous", "candidates": candidates}}
            notes = [f"Open Collective account search for '{args.search_term}' returned multiple candidates; ask the user to confirm or create a new one if needed."]
        else:
            payload = {**payload, "resolution": {"status": "not_found", "candidates": []}}
            notes = [f"No Open Collective account matched '{args.search_term}'. Ask the user whether to create a new one or refine the search."]
        return self._shape_account_search_payload(args, payload), notes

    async def _financial_snapshot_payload(self, args: OpenCollectiveFinancialSnapshotArgs) -> tuple[dict[str, object], list[str]]:
        payload, notes = await self._account_scoped_payload(
            _FINANCIAL_SNAPSHOT_QUERY,
            {
                "slug": args.slug,
                "expenseLimit": args.expense_limit,
                "expenseOffset": args.expense_offset,
                "transactionLimit": args.transaction_limit,
                "transactionOffset": args.transaction_offset,
                "expenseSearchTerm": args.expense_search_term,
                "transactionSearchTerm": args.transaction_search_term,
            },
            args.slug,
        )
        return self._shape_financial_snapshot_payload(args, payload, include_open_liabilities=args.include_open_liabilities), notes

    async def _expense_workflow_payload(self, args: OpenCollectiveExpenseWorkflowArgs) -> tuple[dict[str, object], list[str]]:
        expense_payload = args.expense_payload()
        if args.action == ExpenseWorkflowAction.CREATE:
            payload = await self._post_graphql(
                _CREATE_EXPENSE_MUTATION,
                {
                    "account": args.account.model_dump(mode="json", exclude_none=True) if args.account else None,
                    "expense": expense_payload,
                    "privateComment": args.privateComment,
                },
            )
            return self._shape_expense_operation_payload("accounting_expense_workflow", args, payload, "createExpense"), []

        if args.action == ExpenseWorkflowAction.EDIT:
            payload = await self._post_graphql(_EDIT_EXPENSE_MUTATION, {"expense": expense_payload})
            return self._shape_expense_operation_payload("accounting_expense_workflow", args, payload, "editExpense"), []

        if args.action == ExpenseWorkflowAction.DELETE:
            payload = await self._post_graphql(_DELETE_EXPENSE_MUTATION, {"expense": expense_payload})
            return self._shape_expense_operation_payload("accounting_expense_workflow", args, payload, "deleteExpense"), []

        payload = await self._post_graphql(
            _PROCESS_EXPENSE_MUTATION,
            {
                "expense": expense_payload,
                "action": args.processAction.value if args.processAction else None,
                "message": args.message,
                "paymentParams": args.paymentParams.model_dump(mode="json", exclude_none=True) if args.paymentParams else None,
            },
        )
        return self._shape_expense_operation_payload("accounting_expense_workflow", args, payload, "processExpense"), []

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
            self._shape_expense_operation_payload("accounting_expense_create", args, payload, "createExpense"),
            "live",
            [],
        )

    async def edit_expense(self, args: OpenCollectiveExpenseUpdateArgs) -> tuple[dict[str, object], str, list[str]]:
        payload = await self._post_graphql(
            _EDIT_EXPENSE_MUTATION,
            {"expense": args.expense.model_dump(mode="json", exclude_none=True)},
        )
        return (
            self._shape_expense_operation_payload("accounting_expense_update", args, payload, "editExpense"),
            "live",
            [],
        )

    async def delete_expense(self, args: OpenCollectiveExpenseDeleteArgs) -> tuple[dict[str, object], str, list[str]]:
        payload = await self._post_graphql(
            _DELETE_EXPENSE_MUTATION,
            {"expense": args.expense.model_dump(mode="json", exclude_none=True)},
        )
        return (
            self._shape_expense_operation_payload("accounting_expense_delete", args, payload, "deleteExpense"),
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
            self._shape_expense_operation_payload("accounting_expense_process", args, payload, "processExpense"),
            "live",
            [],
        )

    def _categorize_graphql_error(self, errors: list[dict[str, Any]] | Any) -> str:
        """Categorize GraphQL error for retry decision-making."""
        if not isinstance(errors, list):
            return "UNKNOWN"

        for error in errors:
            if not isinstance(error, dict):
                continue
            msg = error.get("message", "").lower()
            extensions = error.get("extensions", {})
            code = extensions.get("code", "").upper() if isinstance(extensions, dict) else ""

            # Personal-Token is missing a required scope (e.g. "expenses"). Surface this
            # before generic forbidden/permission so callers can route the fix to the user.
            if "personal token is not allowed for operations in scope" in msg or "not allowed for operations in scope" in msg:
                return "SCOPE_MISSING"
            # Check for specific error patterns
            if "authentication" in msg or "unauthorized" in msg or code == "UNAUTHENTICATED":
                return "AUTH"
            if "timeout" in msg or code == "TIMEOUT":
                return "TIMEOUT"
            if "rate" in msg or code == "RATE_LIMITED":
                return "RATE_LIMIT"
            if "validation" in msg or code == "GRAPHQL_VALIDATION_FAILED":
                return "VALIDATION"
            if "not found" in msg:
                return "NOT_FOUND"
            if "permission" in msg or "forbidden" in msg or code == "FORBIDDEN":
                return "PERMISSION"

        # Default to server error if we can't categorize
        return "SERVER_ERROR"

    def _extract_missing_scope(self, errors: list[dict[str, Any]] | Any) -> str | None:
        """Extract the missing scope name from the OC GraphQL Forbidden error message."""
        if not isinstance(errors, list):
            return None
        for error in errors:
            if not isinstance(error, dict):
                continue
            msg = error.get("message", "")
            match = re.search(r"scope\s+\"([^\"]+)\"", msg)
            if match:
                return match.group(1)
        return None

    async def _post_graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.opencollective_pat_token:
            headers["Personal-Token"] = self.settings.opencollective_pat_token

        # Aggressive retry strategy: 5 attempts with exponential backoff
        backoff_times = [1, 2, 4, 8, 16]
        last_error: Exception | None = None

        for attempt in range(5):
            try:
                response = await self._client.post("", json={"query": query, "variables": variables}, headers=headers)

                # Handle HTTP errors
                if response.status_code >= 400:
                    # Check if this is an auth error (don't retry)
                    if response.status_code in (401, 403):
                        raise UpstreamServiceError(
                            response.status_code,
                            "Invalid or missing OPENCOLLECTIVE_PAT_TOKEN. Please set OPENCOLLECTIVE_PAT_TOKEN environment variable.",
                            request="POST /graphql/v2"
                        )
                    # For server errors and rate limits, retry with backoff
                    if response.status_code >= 500 or response.status_code == 429:
                        if attempt < 4:
                            wait_time = backoff_times[attempt]
                            self.logger.warning(f"GraphQL request returned {response.status_code}, retrying in {wait_time}s (attempt {attempt + 1}/5)...")
                            await asyncio.sleep(wait_time)
                            continue
                    raise UpstreamServiceError(response.status_code, response.text, request="POST /graphql/v2")

                # Parse response
                try:
                    payload = response.json()
                except ValueError as e:
                    raise UpstreamServiceError(502, f"Open Collective returned non-JSON payload: {str(e)}", request="POST /graphql/v2") from e

                if not isinstance(payload, dict):
                    raise UpstreamServiceError(502, "Open Collective returned an unexpected non-object payload.", request="POST /graphql/v2")

                # Handle GraphQL errors
                if payload.get("errors"):
                    errors = payload["errors"]
                    error_type = self._categorize_graphql_error(errors)
                    error_msg = json.dumps(errors, ensure_ascii=False)

                    # Auth/permission errors: always raise, even when partial data is present.
                    # Returning partial data for these hides missing scopes and produces silently
                    # incomplete results (e.g. account found but expenses: null due to missing scope).
                    if error_type == "AUTH":
                        raise UpstreamServiceError(
                            401,
                            "Open Collective authentication failed. Please set a valid OPENCOLLECTIVE_PAT_TOKEN.",
                            request="POST /graphql/v2"
                        )

                    if error_type == "SCOPE_MISSING":
                        scope = self._extract_missing_scope(errors) or "(unknown)"
                        raise UpstreamServiceError(
                            403,
                            (
                                f"Open Collective Personal Access Token is missing required scope '{scope}'. "
                                f"Regenerate the token at https://opencollective.com/dashboard/<your-slug>/for-developers/personal-tokens "
                                f"with the '{scope}' scope enabled (along with 'account', 'expenses', and 'transactions' as needed), "
                                f"then update OPENCOLLECTIVE_PAT_TOKEN."
                            ),
                            request="POST /graphql/v2"
                        )

                    if error_type == "PERMISSION":
                        raise UpstreamServiceError(
                            403,
                            f"Open Collective permission denied: {error_msg}",
                            request="POST /graphql/v2"
                        )

                    # For transient errors, return partial data if available
                    data = payload.get("data")
                    if isinstance(data, dict) and data:
                        self.logger.warning(f"GraphQL {error_type} but returning partial data: {error_msg}")
                        return data

                    # For transient errors, retry with backoff
                    if error_type in ("TIMEOUT", "RATE_LIMIT", "SERVER_ERROR"):
                        if attempt < 4:
                            wait_time = backoff_times[attempt]
                            self.logger.warning(f"GraphQL {error_type}, retrying in {wait_time}s (attempt {attempt + 1}/5)...")
                            await asyncio.sleep(wait_time)
                            continue

                    # Final error: raise
                    raise UpstreamServiceError(502, error_msg, request="POST /graphql/v2")

                # Success: return data
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise UpstreamServiceError(502, "Open Collective returned a payload without data.", request="POST /graphql/v2")
                return data

            except httpx.TimeoutException as e:
                last_error = e
                if attempt < 4:
                    wait_time = backoff_times[attempt]
                    self.logger.warning(f"GraphQL timeout, retrying in {wait_time}s (attempt {attempt + 1}/5)...")
                    await asyncio.sleep(wait_time)
                    continue
                raise UpstreamServiceError(504, f"Open Collective API timeout after 5 attempts: {str(e)}", request="POST /graphql/v2") from e
            except UpstreamServiceError:
                raise
            except Exception as e:
                last_error = e
                if attempt < 4:
                    wait_time = backoff_times[attempt]
                    self.logger.warning(f"GraphQL request error: {str(e)}, retrying in {wait_time}s (attempt {attempt + 1}/5)...")
                    await asyncio.sleep(wait_time)
                    continue
                raise UpstreamServiceError(502, f"Open Collective API error: {str(e)}", request="POST /graphql/v2") from e

        # Should not reach here, but just in case
        raise UpstreamServiceError(502, f"GraphQL request failed after 5 attempts", request="POST /graphql/v2")

    async def _account_scoped_payload(
        self,
        query: str,
        variables: dict[str, object],
        requested_slug: str,
    ) -> tuple[dict[str, Any], list[str]]:
        payload = await self._post_graphql(query, variables)
        account = payload.get("account")
        if isinstance(account, dict):
            return payload, []

        resolved_slug, resolution = await self._resolve_account_slug(requested_slug)
        if resolved_slug is None:
            if resolution is not None:
                payload = {**payload, "resolution": resolution}
                recommended = resolution.get("recommended")
                if isinstance(recommended, dict):
                    recommended_slug = recommended.get("slug")
                    recommended_name = recommended.get("name")
                    if isinstance(recommended_slug, str) and recommended_slug:
                        label = f"{recommended_slug}" if not isinstance(recommended_name, str) or not recommended_name else f"{recommended_slug} ({recommended_name})"
                        return payload, [
                            f"No exact Open Collective account matched '{requested_slug}'; closest match is {label}. Ask the user to confirm or create a new one if this is not the intended account."
                        ]
                if resolution.get("status") == "ambiguous":
                    return payload, [f"No exact Open Collective account matched '{requested_slug}'; returning candidate matches. Ask the user to confirm or create a new one if needed."]
                return payload, [f"No Open Collective account matched '{requested_slug}'. Ask the user whether to create a new one or retry with a different label."]
            return payload, []

        resolved_payload = await self._post_graphql(query, {**variables, "slug": resolved_slug})
        return resolved_payload, [f"Resolved Open Collective account '{requested_slug}' to '{resolved_slug}' via accounts search."]

    async def _resolve_account_slug(self, requested_slug: str) -> tuple[str | None, dict[str, Any] | None]:
        # Motivation vs Logic: a missing slug should degrade into resolved candidates and a clear create-or-confirm
        # recommendation rather than a hard upstream error that blocks the whole accounting workflow.
        accounts: list[dict[str, Any]] = []
        for search_term in self._account_search_terms(requested_slug):
            payload = await self._post_graphql(
                _ACCOUNT_SEARCH_QUERY,
                {
                    "searchTerm": search_term,
                    "limit": 10,
                    "offset": 0,
                    "includeArchived": False,
                },
            )
            accounts = self._merge_account_candidates(accounts, self._extract_account_candidates(payload))

        match = self._select_account_candidate(requested_slug, accounts)
        if match is not None:
            slug = match.get("slug")
            if isinstance(slug, str) and slug.strip():
                return slug, None

        recommended = self._recommend_account_candidate(requested_slug, accounts)
        status = "ambiguous" if accounts else "not_found"
        return None, {
            "requestedSlug": requested_slug,
            "status": status,
            "recommended": recommended,
            "candidates": accounts,
        }

    def _account_search_terms(self, requested_slug: str) -> list[str]:
        normalized = requested_slug.strip()
        variants = [normalized]
        variants.append(normalized.replace("-", " "))
        variants.append(normalized.replace("_", " "))
        variants.append(re.sub(r"[^a-zA-Z0-9]+", " ", normalized).strip())
        unique = [term for term in dict.fromkeys(term for term in variants if term)]
        return unique

    def _merge_account_candidates(
        self,
        existing: list[dict[str, Any]],
        new_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        seen_slugs = {self._normalize_account_key(account.get("slug")) for account in existing}
        merged = list(existing)
        for candidate in new_candidates:
            slug_key = self._normalize_account_key(candidate.get("slug"))
            if not slug_key or slug_key in seen_slugs:
                continue
            seen_slugs.add(slug_key)
            merged.append(candidate)
        return merged

    def _recommend_account_candidate(self, requested_slug: str, accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not accounts:
            return None
        requested_key = self._normalize_account_key(requested_slug)
        scored: list[tuple[float, dict[str, Any]]] = []
        for account in accounts:
            slug = self._normalize_account_key(account.get("slug"))
            name = self._normalize_account_key(account.get("name"))
            slug_score = SequenceMatcher(None, requested_key, slug).ratio() if slug else 0.0
            name_score = SequenceMatcher(None, requested_key, name).ratio() if name else 0.0
            score = max(slug_score, name_score)
            scored.append((score, account))
        scored.sort(key=lambda item: item[0], reverse=True)
        score, candidate = scored[0]
        if score >= 0.82:
            return candidate
        return None

    def _extract_account_candidates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        accounts = payload.get("accounts")
        if not isinstance(accounts, dict):
            return []
        nodes = accounts.get("nodes")
        if not isinstance(nodes, list):
            return []
        return [node for node in nodes if isinstance(node, dict)]

    def _select_account_candidate(self, requested_slug: str, accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
        requested_key = self._normalize_account_key(requested_slug)
        exact_slug_matches = [
            account
            for account in accounts
            if self._normalize_account_key(account.get("slug")) == requested_key
        ]
        if len(exact_slug_matches) == 1:
            return exact_slug_matches[0]

        exact_name_matches = [
            account
            for account in accounts
            if self._normalize_account_key(account.get("name")) == requested_key
        ]
        if len(exact_name_matches) == 1:
            return exact_name_matches[0]

        if len(accounts) == 1:
            candidate = accounts[0]
            candidate_slug = self._normalize_account_key(candidate.get("slug"))
            candidate_name = self._normalize_account_key(candidate.get("name"))
            if requested_key in {candidate_slug, candidate_name}:
                return candidate
        return None

    def _normalize_account_key(self, value: object) -> str:
        if not isinstance(value, str):
            return ""
        return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")

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

    def _shape_account_search_payload(self, args: Any, data: dict[str, Any]) -> dict[str, object]:
        accounts = data.get("accounts")
        return {
            "tool": "accounting_collective_search",
            "query": args.model_dump(mode="json", exclude_none=True),
            "accounts": accounts if isinstance(accounts, dict) else None,
            "resolution": data.get("resolution"),
            "raw": data,
        }

    def _shape_financial_snapshot_payload(
        self,
        args: Any,
        data: dict[str, Any],
        *,
        include_open_liabilities: bool,
    ) -> dict[str, object]:
        account = data.get("account")
        snapshot: dict[str, Any] = {
            "tool": "accounting_financial_snapshot",
            "query": args.model_dump(mode="json", exclude_none=True),
            "account": account if isinstance(account, dict) else None,
            "raw": data,
        }
        if include_open_liabilities and isinstance(account, dict):
            snapshot["summary"] = self._summarize_financial_snapshot(account)
        return snapshot

    def _summarize_financial_snapshot(self, account: dict[str, Any]) -> dict[str, Any]:
        expenses = account.get("expenses")
        transactions = account.get("transactions")
        expense_nodes = expenses.get("nodes") if isinstance(expenses, dict) else []
        transaction_nodes = transactions.get("nodes") if isinstance(transactions, dict) else []
        expense_nodes = [node for node in expense_nodes if isinstance(node, dict)] if isinstance(expense_nodes, list) else []
        transaction_nodes = [node for node in transaction_nodes if isinstance(node, dict)] if isinstance(transaction_nodes, list) else []
        open_liability_statuses = {
            "PENDING",
            "APPROVED",
            "SCHEDULED_FOR_PAYMENT",
            "PROCESSING",
            "UNPAID",
            "INCOMPLETE",
            "DRAFT",
            "UNCLEARED",
        }
        open_liabilities = [
            node
            for node in expense_nodes
            if isinstance(node.get("status"), str) and node["status"].upper() in open_liability_statuses
        ]
        stats = account.get("stats")
        native_currency: str | None = None
        if isinstance(stats, dict):
            balance = stats.get("balance")
            if isinstance(balance, dict):
                native_currency = balance.get("currency") if isinstance(balance.get("currency"), str) else None
        return {
            "expense_count": expenses.get("totalCount") if isinstance(expenses, dict) else None,
            "transaction_count": transactions.get("totalCount") if isinstance(transactions, dict) else None,
            "open_liability_count": len(open_liabilities),
            "open_liability_amount_total": self._sum_amounts(open_liabilities),
            "open_liability_currency": native_currency,
            "open_liabilities": open_liabilities,
            "recent_expenses": expense_nodes,
            "recent_transactions": transaction_nodes,
        }

    def _sum_amounts(self, nodes: list[dict[str, Any]]) -> float:
        total = 0.0
        for node in nodes:
            # amountV2 is the current Amount object field (value in dollars, valueInCents in cents)
            amount_v2 = node.get("amountV2")
            if isinstance(amount_v2, dict):
                cents = amount_v2.get("valueInCents")
                if isinstance(cents, (int, float)):
                    total += float(cents) / 100.0
                    continue
                value = amount_v2.get("value")
                if isinstance(value, (int, float)):
                    total += float(value)
                    continue
            # Legacy amount field: Int (cents) on Expense, or Amount dict on Transaction
            amount = node.get("amount")
            if isinstance(amount, (int, float)):
                total += float(amount) / 100.0
                continue
            if isinstance(amount, dict):
                cents = amount.get("valueInCents")
                if isinstance(cents, (int, float)):
                    total += float(cents) / 100.0
                    continue
                value = amount.get("value")
                if isinstance(value, (int, float)):
                    total += float(value)
        return total

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
