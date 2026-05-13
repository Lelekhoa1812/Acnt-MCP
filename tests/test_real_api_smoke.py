"""
Integration smoke tests against the live Open Collective API.

These tests verify that each new tool can reach the API and return a
well-formed response. They assert on response *shape*, not on specific
account data, so they pass regardless of which collectives exist.

Run with real credentials:
    env $(grep -v ^# .env.final | xargs) pytest tests/test_real_api_smoke.py -v -m integration
"""
from __future__ import annotations

import logging
import os

import pytest

from app.config import Settings
from app.store import AppKeyValueStore
from app.tool.accounting import (
    AccountingService,
    AccountReferenceInput,
    OpenCollectiveCollectiveListArgs,
    OpenCollectivePayeeListArgs,
    OpenCollectivePayeeViewArgs,
    OpenCollectiveAccountSearchArgs,
)


def _pat_token() -> str | None:
    return os.environ.get("OPENCOLLECTIVE_PAT_TOKEN")


def _settings() -> Settings:
    return Settings(
        ACNT_LOG_LEVEL="warning",
        ACNT_REDIS_FALLBACK_ENABLED=True,
        OPENCOLLECTIVE_PAT_TOKEN=_pat_token() or "",
        OPENCOLLECTIVE_GRAPHQL_URL="https://api.opencollective.com/graphql/v2",
    )


@pytest.fixture()
async def svc():
    settings = _settings()
    store = AppKeyValueStore(settings=settings, logger=logging.getLogger("smoke"))
    service = AccountingService(settings=settings, key_value_store=store, logger=logging.getLogger("smoke"))
    yield service
    await service.close()


pytestmark = pytest.mark.integration


@pytest.mark.anyio
@pytest.mark.skipif(not _pat_token(), reason="OPENCOLLECTIVE_PAT_TOKEN not set")
async def test_collective_search_returns_valid_resolution(svc: AccountingService) -> None:
    data, _, _ = await svc.collective_search(OpenCollectiveAccountSearchArgs(search_term="opencollective"))
    assert "resolution" in data
    assert data["resolution"]["status"] in {"recommended", "ambiguous", "not_found"}
    assert "accounts" in data


@pytest.mark.anyio
@pytest.mark.skipif(not _pat_token(), reason="OPENCOLLECTIVE_PAT_TOKEN not set")
async def test_collective_list_returns_paginated_accounts(svc: AccountingService) -> None:
    data, _, _ = await svc.collective_list(OpenCollectiveCollectiveListArgs(limit=5))
    assert "accounts" in data
    accounts = data["accounts"]
    assert isinstance(accounts, dict)
    assert "totalCount" in accounts
    assert isinstance(accounts["totalCount"], int)
    assert accounts["totalCount"] >= 0
    assert "nodes" in accounts


@pytest.mark.anyio
@pytest.mark.skipif(not _pat_token(), reason="OPENCOLLECTIVE_PAT_TOKEN not set")
async def test_collective_list_with_type_filter(svc: AccountingService) -> None:
    data, _, _ = await svc.collective_list(OpenCollectiveCollectiveListArgs(limit=5, type=["COLLECTIVE"]))
    assert "accounts" in data
    nodes = data["accounts"].get("nodes", [])
    for node in nodes:
        assert node.get("type") == "COLLECTIVE"


@pytest.mark.anyio
@pytest.mark.skipif(not _pat_token(), reason="OPENCOLLECTIVE_PAT_TOKEN not set")
async def test_payee_list_returns_user_and_org_accounts(svc: AccountingService) -> None:
    data, _, _ = await svc.payee_list(OpenCollectivePayeeListArgs(limit=5))
    assert "accounts" in data
    accounts = data["accounts"]
    assert isinstance(accounts, dict)
    assert "totalCount" in accounts
    assert isinstance(accounts["totalCount"], int)


@pytest.mark.anyio
@pytest.mark.skipif(not _pat_token(), reason="OPENCOLLECTIVE_PAT_TOKEN not set")
async def test_payee_view_returns_account_detail(svc: AccountingService) -> None:
    # "opencollective" is a well-known public organisation on OC
    data, _, _ = await svc.payee_view(OpenCollectivePayeeViewArgs(slug="opencollective"))
    assert "account" in data
    if data["account"] is not None:
        assert "slug" in data["account"]
        assert "name" in data["account"]
        assert "type" in data["account"]
