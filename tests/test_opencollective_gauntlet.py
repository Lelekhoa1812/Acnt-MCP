from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.store import AppKeyValueStore
from app.tool.accounting import (
    AccountingService,
    OpenCollectiveAccountSearchArgs,
    OpenCollectiveBudgetLookupArgs,
    OpenCollectiveExpenseCreateArgs,
    OpenCollectiveExpenseDeleteArgs,
    OpenCollectiveExpenseListArgs,
    OpenCollectiveExpenseProcessArgs,
    OpenCollectiveExpenseUpdateArgs,
    OpenCollectiveExpenseWorkflowArgs,
    OpenCollectiveFinancialSnapshotArgs,
    OpenCollectiveTransactionAllArgs,
)


def _settings() -> Settings:
    return Settings(
        HTH_LOG_LEVEL="warning",
        HTH_REDIS_FALLBACK_ENABLED=True,
        HTH_REDIS_URL="redis://127.0.0.1:65535",
        OPENCOLLECTIVE_CLIENT_ID="oc-client-id",
        OPENCOLLECTIVE_CLIENT_SECRET="oc-client-secret",
        OPENCOLLECTIVE_PAT_TOKEN="oc-personal-token",
        OPENCOLLECTIVE_GRAPHQL_URL="https://api.opencollective.com/graphql/v2",
    )


def _load_gauntlet_fixture() -> dict[str, object]:
    fixture_path = Path(__file__).resolve().parents[1] / "mock" / "opencollective-accounting-gauntlet.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


@pytest.mark.anyio
async def test_opencollective_gauntlet_exercises_all_accounting_tools() -> None:
    fixture = _load_gauntlet_fixture()
    scenario = fixture["scenario"]
    search_cases = fixture["search_cases"]
    ledger = fixture["ledger"]
    mutations = fixture["mutations"]

    seen_operations: list[dict[str, object]] = []
    budget_lookup_calls = 0

    # Motivation vs Logic: keep the dispatcher fixture-driven so a single noisy
    # scenario can exercise every accounting path without duplicating payload
    # builders across individual tests.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        query = body["query"]
        variables = body["variables"]
        seen_operations.append(
            {
                "query": query,
                "variables": variables,
            }
        )

        if "query AccountSearch" in query:
            search_term = variables["searchTerm"]
            if search_term in {"Aurora OSS", "aurora-oss", "aurora oss", "Aurora Open Source Collective"}:
                return httpx.Response(200, json={"data": {"accounts": search_cases["recommended"]["accounts"]}})
            if search_term == "North Star":
                return httpx.Response(200, json={"data": {"accounts": search_cases["ambiguous"]["accounts"]}})
            if search_term == "Ghost Orbit":
                return httpx.Response(200, json={"data": {"accounts": search_cases["missing"]["accounts"]}})
            if search_term == "Aurora Open Source":
                return httpx.Response(200, json={"data": {"accounts": search_cases["recommended"]["accounts"]}})
            return httpx.Response(200, json={"data": {"accounts": search_cases["missing"]["accounts"]}})

        if "query BudgetLookup" in query:
            nonlocal budget_lookup_calls
            budget_lookup_calls += 1
            if budget_lookup_calls == 1:
                return httpx.Response(200, json=ledger["budget_lookup"]["initial"])
            return httpx.Response(200, json=ledger["budget_lookup"]["resolved"])

        if "query FinancialSnapshot" in query:
            return httpx.Response(200, json=ledger["financial_snapshot"])

        if "query ExpenseList" in query:
            assert variables["searchTerm"] == "hosting"
            return httpx.Response(200, json=ledger["expense_list"])

        if "query TransactionAll" in query:
            assert variables["searchTerm"] == "donation"
            return httpx.Response(200, json=ledger["transaction_all"])

        if "createExpense" in query and "processExpense" not in query and "editExpense" not in query and "deleteExpense" not in query:
            if "publicId" in json.dumps(variables):
                return httpx.Response(200, json=mutations["direct_create"])
            return httpx.Response(200, json=mutations["workflow_create"])

        if "editExpense" in query:
            if variables["expense"].get("description", "").startswith("Travel stipend"):
                return httpx.Response(200, json=mutations["direct_edit"])
            return httpx.Response(200, json=mutations["workflow_edit"])

        if "deleteExpense" in query:
            if variables["expense"].get("id") == "exp-3002":
                return httpx.Response(200, json=mutations["direct_delete"])
            return httpx.Response(200, json=mutations["workflow_delete"])

        if "processExpense" in query:
            if variables["expense"].get("id") == "exp-1003":
                return httpx.Response(200, json=mutations["direct_process"])
            return httpx.Response(200, json=mutations["workflow_process"])

        return httpx.Response(400, json={"error": "unexpected query"})

    settings = _settings()
    store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test"))
    service = AccountingService(settings=settings, key_value_store=store, logger=logging.getLogger("test"))
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.opencollective_graphql_url)

    recommended_search, _, notes = await service.account_search(OpenCollectiveAccountSearchArgs(search_term="Aurora OSS"))
    ambiguous_search, _, _ = await service.account_search(OpenCollectiveAccountSearchArgs(search_term="North Star"))
    missing_search, _, _ = await service.account_search(OpenCollectiveAccountSearchArgs(search_term="Ghost Orbit"))

    budget_data, _, budget_notes = await service.budget_lookup(OpenCollectiveBudgetLookupArgs(slug="aurora-oss"))
    snapshot_data, _, snapshot_notes = await service.financial_snapshot(OpenCollectiveFinancialSnapshotArgs(slug="aurora-oss"))
    expense_list_data, _, _ = await service.expense_list(OpenCollectiveExpenseListArgs(slug="aurora-oss", search_term="hosting"))
    transaction_data, _, _ = await service.transaction_all(OpenCollectiveTransactionAllArgs(slug="aurora-oss", search_term="donation"))

    workflow_created, _, _ = await service.expense_workflow(
        OpenCollectiveExpenseWorkflowArgs(
            action="CREATE",
            account={"slug": "aurora-oss"},
            expense={
                "description": "Accessibility sprint GPU grant",
                "type": "INVOICE",
                "payee": {"slug": "river-labs"},
                "payoutMethod": {"type": "BANK_TRANSFER", "name": "Manual transfer", "data": {"content": "ANZ-001"}},
            },
            privateComment="Please attach the invoice and confirm the payout window.",
        )
    )
    workflow_processed, _, _ = await service.expense_workflow(
        OpenCollectiveExpenseWorkflowArgs(
            action="PROCESS",
            expense={"id": "exp-1001"},
            processAction="APPROVE",
            message="Approved after confirming the release-week deployment window.",
            paymentParams={"forceManual": True, "paymentMethodService": "WISE", "totalAmountPaidInHostCurrency": 2400},
        )
    )

    direct_created, _, _ = await service.create_expense(
        OpenCollectiveExpenseCreateArgs(
            account={"slug": "aurora-oss"},
            expense={
                "description": "Travel stipend for community maintainer",
                "type": "INVOICE",
                "payee": {"slug": "river-labs"},
                "payoutMethod": {"type": "BANK_TRANSFER", "name": "Manual transfer", "data": {"content": "ANZ-001"}},
            },
            privateComment="Attach receipts before the payout window opens.",
        )
    )
    direct_updated, _, _ = await service.edit_expense(
        OpenCollectiveExpenseUpdateArgs(
            expense={
                "id": "exp-3001",
                "description": "Travel stipend for community maintainer - updated",
                "type": "INVOICE",
                "payee": {"slug": "river-labs"},
                "payoutMethod": {"type": "BANK_TRANSFER"},
            }
        )
    )
    direct_deleted, _, _ = await service.delete_expense(OpenCollectiveExpenseDeleteArgs(expense={"id": "exp-3002"}))
    direct_processed, _, _ = await service.process_expense(
        OpenCollectiveExpenseProcessArgs(
            expense={"id": "exp-1003"},
            action="APPROVE",
            message="Approved after confirming milestone completion.",
            paymentParams={"forceManual": False, "paymentMethodService": "WISE"},
        )
    )

    assert recommended_search["resolution"]["status"] == "recommended"
    assert recommended_search["resolution"]["recommended"]["slug"] == "aurora-oss"
    assert ambiguous_search["resolution"]["status"] == "ambiguous"
    assert missing_search["resolution"]["status"] == "not_found"
    assert budget_data["account"]["slug"] == "aurora-oss"
    assert budget_data["account"]["stats"]["balance"]["valueInCents"] == 4825000
    assert budget_notes == ["Resolved Open Collective account 'aurora-oss' to 'aurora-oss' via accounts search."]
    assert snapshot_data["summary"]["open_liability_count"] == 3
    assert snapshot_notes == []
    assert expense_list_data["account"]["expenses"]["totalCount"] == 6
    assert transaction_data["account"]["transactions"]["totalCount"] == 5
    assert workflow_created["expense"]["status"] == "PENDING"
    assert workflow_processed["expense"]["status"] == "APPROVED"
    assert direct_created["expense"]["status"] == "PENDING"
    assert direct_updated["expense"]["status"] == "APPROVED"
    assert direct_deleted["expense"]["status"] == "DELETED"
    assert direct_processed["expense"]["status"] == "APPROVED"

    observed_queries = "\n".join(str(item["query"]) for item in seen_operations)
    assert set(scenario["tool_coverage"]) == {
        "accounting_account_search",
        "accounting_budget_lookup",
        "accounting_financial_snapshot",
        "accounting_expense_list",
        "accounting_transaction_all",
        "accounting_expense_workflow",
        "accounting_expense_create",
        "accounting_expense_update",
        "accounting_expense_delete",
        "accounting_expense_process",
    }
    assert "query AccountSearch" in observed_queries
    assert "query BudgetLookup" in observed_queries
    assert "query FinancialSnapshot" in observed_queries
    assert "query ExpenseList" in observed_queries
    assert "query TransactionAll" in observed_queries
    assert "createExpense" in observed_queries
    assert "editExpense" in observed_queries
    assert "deleteExpense" in observed_queries
    assert "processExpense" in observed_queries

    await service.close()
