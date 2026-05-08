from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import Settings
from app.tool.accounting import (
    AccountingService,
    OpenCollectiveBudgetLookupArgs,
    OpenCollectiveExpenseCreateArgs,
    OpenCollectiveExpenseDeleteArgs,
    OpenCollectiveExpenseProcessArgs,
    OpenCollectiveExpenseUpdateArgs,
    OpenCollectiveExpenseListArgs,
    OpenCollectiveTransactionAllArgs,
)
from app.tool.ecommerce import EcommerceService, EbayCategoryTreeArgs, EbayItemDetailArgs, EbayItemSearchArgs
from app.prompt.registry import build_registry_prompt_policy
from app.tool.retail import OpenLibraryBookSearchArgs, OpenLibraryIsbnLookupArgs, OpenLibraryService, OpenLibrarySubjectListArgs
from app.store import AppKeyValueStore


def _settings() -> Settings:
    return Settings(
        HTH_LOG_LEVEL="warning",
        HTH_REDIS_FALLBACK_ENABLED=True,
        HTH_REDIS_URL="redis://127.0.0.1:65535",
        EBAY_CLIENT_ID="client-id",
        EBAY_CLIENT_SECRET="client-secret",
        EBAY_MARKETPLACE_ID="EBAY_AU",
        EBAY_ENVIRONMENT="production",
        OPENCOLLECTIVE_CLIENT_ID="oc-client-id",
        OPENCOLLECTIVE_CLIENT_SECRET="oc-client-secret",
        OPENCOLLECTIVE_PAT_TOKEN="oc-personal-token",
        OPENCOLLECTIVE_GRAPHQL_URL="https://api.opencollective.com/graphql/v2",
    )


@pytest.mark.anyio
async def test_ebay_service_search_detail_and_category_tree_shapes_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/identity/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "token-123", "expires_in": 3600})
        if request.url.path.endswith("/buy/browse/v1/item_summary/search"):
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "itemSummaries": [{"itemId": "100", "title": "Sample Chair", "price": {"value": "79.99", "currency": "AUD"}}],
                },
            )
        if request.url.path.endswith("/buy/browse/v1/item/100"):
            return httpx.Response(200, json={"itemId": "100", "title": "Sample Chair", "seller": {"username": "seller-one"}})
        if request.url.path.endswith("/commerce/taxonomy/v1/category_tree/0"):
            return httpx.Response(200, json={"categoryTreeId": "0", "rootCategoryNode": {"category": {"categoryId": "123", "categoryName": "Furniture"}}})
        return httpx.Response(404, json={"error": "not found"})

    settings = _settings()
    store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test"))
    service = EcommerceService(settings=settings, key_value_store=store, logger=logging.getLogger("test"))
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.resolved_ebay_base_url)

    search_data, _, _ = await service.item_search(EbayItemSearchArgs(query="chair"))
    detail_data, _, _ = await service.item_detail(EbayItemDetailArgs(item_id="100"))
    tree_data, _, _ = await service.category_tree(EbayCategoryTreeArgs(category_tree_id="0"))

    assert search_data["items"][0]["title"] == "Sample Chair"
    assert detail_data["item"]["seller"]["username"] == "seller-one"
    assert tree_data["categoryTree"]["rootCategoryNode"]["category"]["categoryName"] == "Furniture"
    assert any(req.url.path.endswith("/identity/v1/oauth2/token") for req in seen)
    search_request = next(req for req in seen if req.url.path.endswith("/buy/browse/v1/item_summary/search"))
    assert search_request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_AU"
    assert search_request.headers["Authorization"] == "Bearer token-123"

    await service.close()


@pytest.mark.anyio
async def test_opencollective_service_uses_personal_token_and_graphql_shapes_payloads() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append(
            {
                "headers": dict(request.headers),
                "body": body,
                "path": request.url.path,
            }
        )
        if body["query"].startswith("query ExpenseList"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "account": {
                            "id": "acct-1",
                            "slug": "webpack",
                            "name": "webpack",
                            "expenses": {
                                "totalCount": 2,
                                "nodes": [{"id": "exp-1", "amount": 42, "currency": "USD", "description": "Hosting"}],
                            },
                        }
                    }
                },
            )
        if body["query"].startswith("query TransactionAll"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "account": {
                            "id": "acct-1",
                            "slug": "webpack",
                            "name": "webpack",
                            "transactions": {
                                "totalCount": 3,
                                "nodes": [{"id": "txn-1", "amount": 50, "currency": "USD", "description": "Donation"}],
                            },
                        }
                    }
                },
            )
        if body["query"].startswith("query BudgetLookup"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "account": {
                            "id": "acct-1",
                            "slug": "webpack",
                            "name": "webpack",
                            "stats": {"balance": 1234, "yearlyBudget": 6000, "monthlySpending": 250},
                        }
                    }
                },
            )
        return httpx.Response(400, json={"error": "unexpected query"})

    settings = _settings()
    store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test"))
    service = AccountingService(settings=settings, key_value_store=store, logger=logging.getLogger("test"))
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.opencollective_graphql_url)

    expense_data, _, _ = await service.expense_list(OpenCollectiveExpenseListArgs(slug="webpack"))
    txn_data, _, _ = await service.transaction_all(OpenCollectiveTransactionAllArgs(slug="webpack"))
    budget_data, _, _ = await service.budget_lookup(OpenCollectiveBudgetLookupArgs(slug="webpack"))

    assert expense_data["account"]["expenses"]["totalCount"] == 2
    assert txn_data["account"]["transactions"]["totalCount"] == 3
    assert budget_data["account"]["stats"]["balance"] == 1234
    assert seen[0]["headers"]["personal-token"] == "oc-personal-token"
    assert str(seen[0]["path"]).rstrip("/") == "/graphql/v2"

    await service.close()


@pytest.mark.anyio
async def test_opencollective_service_mutations_support_create_edit_delete_and_process() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        query = body["query"]
        base = {
            "id": "ex_111",
            "slug": "expense-111",
            "status": "PENDING",
            "description": "Test expense",
            "type": "INVOICE",
            "amount": 100,
            "currency": "USD",
        }
        if "createExpense" in query:
            return httpx.Response(200, json={"data": {"createExpense": base}})
        if "editExpense" in query:
            updated = {**base, "status": "APPROVED", "description": "Updated description"}
            return httpx.Response(200, json={"data": {"editExpense": updated}})
        if "deleteExpense" in query:
            deleted = {**base, "status": "DELETED"}
            return httpx.Response(200, json={"data": {"deleteExpense": deleted}})
        if "processExpense" in query:
            processed = {**base, "status": "APPROVED", "privateMessage": "Approved via tool"}
            return httpx.Response(200, json={"data": {"processExpense": processed}})
        return httpx.Response(400, json={"error": "unexpected query"})

    settings = _settings()
    store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test"))
    service = AccountingService(settings=settings, key_value_store=store, logger=logging.getLogger("test"))
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=settings.opencollective_graphql_url.rstrip("/"),
    )

    create_args = OpenCollectiveExpenseCreateArgs(
        account={"slug": "webpack"},
        expense={
            "description": "Create test",
            "type": "INVOICE",
            "payee": {"slug": "webpack"},
            "payoutMethod": {"type": "OTHER", "name": "Manual payout", "data": {"content": "manual"}},
        },
        privateComment="created in test",
    )
    created, _, _ = await service.create_expense(create_args)
    assert created["expense"]["status"] == "PENDING"

    update_args = OpenCollectiveExpenseUpdateArgs(
        expense={
            "id": "ex_111",
            "description": "Update test",
            "type": "INVOICE",
            "payee": {"slug": "webpack"},
            "payoutMethod": {"type": "OTHER"},
        },
    )
    updated, _, _ = await service.edit_expense(update_args)
    assert updated["expense"]["status"] == "APPROVED"

    deleted, _, _ = await service.delete_expense(OpenCollectiveExpenseDeleteArgs(expense={"id": "ex_111"}))
    assert deleted["expense"]["status"] == "DELETED"

    processed, _, _ = await service.process_expense(
        OpenCollectiveExpenseProcessArgs(
            expense={"id": "ex_111"},
            action="APPROVE",
            message="Approving via test",
        )
    )
    assert processed["expense"]["status"] == "APPROVED"

    await service.close()


@pytest.mark.anyio
async def test_openlibrary_service_search_isbn_and_subject() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search.json":
            assert request.url.params["q"] == "octavia butler"
            return httpx.Response(
                200,
                json={"numFound": 1, "docs": [{"title": "Parable of the Sower", "author_name": ["Octavia E. Butler"]}]},
            )
        if request.url.path == "/isbn/9780446675536.json":
            return httpx.Response(200, json={"title": "Parable of the Sower", "isbn_13": ["9780446675536"]})
        if request.url.path == "/subjects/climate_fiction.json":
            return httpx.Response(200, json={"works": [{"title": "Parable of the Sower"}]})
        return httpx.Response(404, json={"error": "not found"})

    settings = _settings()
    store = AppKeyValueStore(settings=settings, logger=logging.getLogger("test"))
    service = OpenLibraryService(settings=settings, key_value_store=store, logger=logging.getLogger("test"))
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://openlibrary.org")

    search_data, _, _ = await service.book_search(OpenLibraryBookSearchArgs(query="octavia butler", limit=5))
    isbn_data, _, _ = await service.isbn_lookup(OpenLibraryIsbnLookupArgs(isbn="978-0446675536"))
    subject_data, _, _ = await service.subject_list(OpenLibrarySubjectListArgs(subject="climate fiction"))

    assert search_data["search"]["numFound"] == 1
    assert isbn_data["book"]["isbn_13"][0] == "9780446675536"
    assert subject_data["subjectList"]["works"][0]["title"] == "Parable of the Sower"

    await service.close()


def test_registry_prompt_includes_new_domain_examples() -> None:
    policy = build_registry_prompt_policy("Find eBay listings", intent_classes=["ecommerce"], context_mode="normal")
    assert policy.route.plugin_intents == ("ecommerce",)
    assert "E-Commerce Example" in policy.examples
    assert "Use eBay tools" in "\n".join(policy.behavior_rules)
