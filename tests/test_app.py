from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent.engine import AgentEngine, AgentRun
from app.tool.currency.service import CurrencyProviderError
from app.config import Settings, UpstreamServiceError
from app.main import create_app
from app.schemas import (
    AgentDebugGrounding,
    AgentDebugIntent,
    AgentDebugPayload,
    AgentDebugPlan,
    AgentDebugPlanStep,
    AgentDebugRetrieval,
    MemoCache,
    PlanStatus,
    PlanStep,
    ProductListItemDto,
    ProductListItemDtoPagedResponse,
    ProductVariantDto,
    ProductVariationDto,
    ProductVariationOptionDto,
    ToolTrace,
)

TEST_REDIS_URL = "redis://127.0.0.1:65535"
MCP_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def build_client() -> TestClient:
    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        public_base_url=None,
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        mock_catalog_path="./mock/product-catalog.json",
        mock_details_path="./mock/product-details.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
    )
    return TestClient(create_app(settings))


def build_cloud_client() -> TestClient:
    settings = Settings(
        local_harmonise=False,
        cloud_harmonise_endpoint="https://cloud.harmonise.test",
        cloud_harmonise_api="test-api-key",
        cloud_harmonise_image="https://images.harmonise.test",
        log_level="debug",
        public_base_url=None,
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
    )
    return TestClient(create_app(settings))


def build_mcp_auth_client() -> TestClient:
    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        public_base_url="https://hth.example.test",
        server_website_url=None,
        server_logo_url=None,
        mcp_allowed_hosts="testserver",
        mock_catalog_path="./mock/product-catalog.json",
        mock_details_path="./mock/product-details.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
        mcp_bearer_token="test-mcp-token",
        mcp_oauth_enabled=True,
    )
    return TestClient(create_app(settings))


def test_health_endpoint_reports_local_harmonise_mode() -> None:
    with build_client() as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data_source"] == "harmonise_local"
    assert payload["session_cache_backend"] in ("redis", "memory")
    assert isinstance(payload["redis_client_connected"], bool)
    assert payload["redis_fallback_enabled"] is True
    assert payload["local_chat_memory_enabled"] is True
    assert payload["local_chat_memory_turns"] == 6


def test_mcp_unauthorized_response_advertises_oauth_metadata() -> None:
    with build_mcp_auth_client() as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0.0"},
                },
            },
            headers={"accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401
    assert (
        response.headers["www-authenticate"]
        == 'Bearer realm="mcp", resource_metadata="https://hth.example.test/.well-known/oauth-protected-resource"'
    )


def test_mcp_oauth_metadata_and_token_bridge() -> None:
    with build_mcp_auth_client() as client:
        protected = client.get("/.well-known/oauth-protected-resource")
        authorization_server = client.get("/.well-known/oauth-authorization-server")
        registered = client.post("/oauth/register", json={"client_name": "pytest"})
        client_payload = registered.json()
        authorized = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_payload["client_id"],
                "redirect_uri": "https://claude.ai/callback",
                "state": "state-1",
            },
            follow_redirects=False,
        )
        query = parse_qs(urlparse(authorized.headers["location"]).query)
        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": "https://claude.ai/callback",
            },
        )

    assert protected.status_code == 200
    assert protected.json()["resource"] == "https://hth.example.test/mcp"
    assert authorization_server.status_code == 200
    assert authorization_server.json()["authorization_endpoint"] == "https://hth.example.test/oauth/authorize"
    assert authorization_server.json()["client_id_metadata_document_supported"] is True
    assert registered.status_code == 201
    assert authorized.status_code == 302
    assert query["state"] == ["state-1"]
    assert token.status_code == 200
    assert token.json()["access_token"] == "test-mcp-token"
    assert token.json()["token_type"] == "Bearer"


def test_mcp_browser_origins_receive_cors_headers() -> None:
    with build_mcp_auth_client() as client:
        protected = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"Origin": "https://claude.ai"},
        )
        challenge = client.get(
            "/mcp",
            headers={"Origin": "https://claude.ai"},
        )
        preflight = client.options(
            "/mcp",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )

    assert protected.headers["access-control-allow-origin"] == "https://claude.ai"
    assert challenge.headers["access-control-allow-origin"] == "https://claude.ai"
    assert challenge.headers["access-control-expose-headers"] == "WWW-Authenticate"
    assert preflight.headers["access-control-allow-origin"] == "https://claude.ai"
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()


def test_mcp_oauth_bridge_stays_available_when_flag_is_off() -> None:
    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        mock_catalog_path="./mock/product-catalog.json",
        mock_details_path="./mock/product-details.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=False,
        public_base_url="https://hth.example.test",
        mcp_bearer_token="test-mcp-token",
        mcp_oauth_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        root = client.get("/")
        registered = client.post("/oauth/register", json={"client_name": "pytest"})
        client_payload = registered.json()
        authorized = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_payload["client_id"],
                "redirect_uri": "https://claude.ai/callback",
            },
            follow_redirects=False,
        )
        query = parse_qs(urlparse(authorized.headers["location"]).query)
        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": "https://claude.ai/callback",
            },
        )

    assert root.json()["mcp_oauth_enabled"] is True
    assert registered.status_code == 201
    assert authorized.status_code == 302
    assert token.status_code == 200
    assert token.json()["access_token"] == "test-mcp-token"


def test_mcp_oauth_endpoint_aliases_remain_supported() -> None:
    with build_mcp_auth_client() as client:
        registered = client.post("/register", json={"client_name": "pytest"})
        client_payload = registered.json()
        authorized = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_payload["client_id"],
                "redirect_uri": "https://claude.ai/callback",
            },
            follow_redirects=False,
        )
        query = parse_qs(urlparse(authorized.headers["location"]).query)
        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": "https://claude.ai/callback",
            },
        )

    assert registered.status_code == 201
    assert authorized.status_code == 302
    assert token.status_code == 200
    assert token.json()["access_token"] == "test-mcp-token"


def test_tools_endpoint_lists_stock_and_plugin_tools() -> None:
    with build_client() as client:
        response = client.get("/api/v1/tools")

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["tools"]}
    assert all(MCP_TOOL_NAME_PATTERN.fullmatch(name) for name in tool_names)
    assert "stock_get_departments" in tool_names
    assert "stock_get_categories" in tool_names
    assert "stock_search_catalogue" in tool_names
    assert "stock_inventory_snapshot" in tool_names
    assert "resolver_disambiguate_candidates" in tool_names
    assert "weather_current" in tool_names
    assert "news_search" in tool_names
    assert "currency_convert" in tool_names


def test_tools_endpoint_hides_metadata_tools_in_cloud_mode() -> None:
    with build_cloud_client() as client:
        response = client.get("/api/v1/tools")

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["tools"]}
    assert all(MCP_TOOL_NAME_PATTERN.fullmatch(name) for name in tool_names)
    assert "stock_get_departments" not in tool_names
    assert "stock_get_categories" not in tool_names
    assert "stock_search_catalogue" in tool_names
    assert "stock_get_product" in tool_names


def test_search_catalogue_tool_runs_through_local_harmonise() -> None:
    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock.search_catalogue",
                "args": {"page": 1, "pageSize": 10, "search": "white gloss dance floor"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "stock.search_catalogue"
    names = [item["name"] for item in payload["data"]["items"]]
    assert "Dance Floor - White Gloss " in names
    assert payload["plan_status"]["status"] == "complete"
    assert payload["memo_update"]["tool"] == "stock.search_catalogue"
    assert payload["validation"]["actual_rows"] is not None


def test_inventory_snapshot_tool_returns_compact_rows_for_table_answers() -> None:
    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock.inventory_snapshot",
                "args": {"page": 1, "pageSize": 100},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "stock.inventory_snapshot"
    assert payload["data"]["coverage"]["matchedProducts"] == 40
    assert payload["data"]["coverage"]["matchedPages"] == 1
    assert payload["data"]["coverage"]["enrichedVariants"] == 60

    row = next(item for item in payload["data"]["rows"] if item["sku"] == "fl-ca-ca-10m")
    assert row["size"] == "1 x 1 x 0.01 m"
    assert row["stock"] is not None
    assert "total=" not in row["stock"]
    assert "Overall" in row["stock"]
    assert "in stock" in row["stock"]
    assert "10m Hex Carpet Set - Onyx" in row["attributeEvidence"]
    assert any("sales note" in spec.lower() for spec in row["knownSpecs"])
    assert payload["plan_status"]["steps"][0]["tool"] == "stock.inventory_snapshot"
    assert payload["validation"]["expected_rows"] is not None


def test_public_tool_name_resolves_to_internal_inventory_snapshot() -> None:
    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock_inventory_snapshot",
                "args": {"page": 1, "pageSize": 100},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "stock.inventory_snapshot"
    assert payload["data"]["coverage"]["matchedProducts"] >= 1
    assert payload["data"]["rows"]


def test_rest_tool_call_persists_plan_todo_and_memo_cache_across_session_calls() -> None:
    session_id = f"tool-plan-{uuid4()}"

    with build_client() as client:
        first = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock.search_catalogue",
                "sessionId": session_id,
                "args": {"page": 1, "pageSize": 5, "search": "dance floor"},
            },
        )
        second = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock.search_catalogue",
                "sessionId": session_id,
                "args": {"page": 1, "pageSize": 5, "search": "dance floor"},
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["plan_status"]["steps"][0]["status"] == "done"
    assert len(second_payload["plan_status"]["steps"]) >= 2
    assert second_payload["plan_status"]["memo"]["aggregates"]["entry_count"] >= 2


def test_rest_tool_endpoint_returns_structured_bad_request_for_invalid_args() -> None:
    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock.get_product",
                "args": {},
            },
        )

    assert response.status_code == 400
    assert "Invalid arguments for 'stock.get_product'" in response.json()["detail"]


def test_variant_evidence_tool_resolves_by_sku() -> None:
    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock.get_variant_evidence",
                "args": {"sku": "fl-ca-ca-10m"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "stock.extract_variant_evidence"
    assert payload["data"]["sku"] == "fl-ca-ca-10m"
    assert payload["data"]["dimensions"]["length"] == 1.0
    assert payload["data"]["dimensions"]["width"] == 1.0


def test_variant_evidence_tool_rejects_bare_variant_id_with_clear_guidance() -> None:
    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "stock.get_variant_evidence",
                "args": {"variantId": "d2a50000-0e48-c047-e1bc-08dde35c3772"},
            },
        )

    assert response.status_code == 400
    payload = response.json()
    assert "variantId alone cannot resolve product details" in payload["detail"]
    assert "variants[].sku" in payload["detail"]


def test_disambiguate_candidates_returns_selectable_options_for_small_ambiguity() -> None:
    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "resolver.disambiguate_candidates",
                "args": {"query": "chair", "limit": 10},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "resolver.disambiguate_candidates"
    assert payload["data"]["status"] == "needs_clarification"
    assert payload["data"]["selection_mode"] == "select_option"
    assert payload["data"]["total_matches"] is not None
    assert payload["data"]["total_matches"] <= 10
    assert 1 <= len(payload["data"]["options"]) <= 10


def test_disambiguate_candidates_resolves_single_product_family_without_clarification(monkeypatch) -> None:
    async def fake_search_catalogue(self, args):  # noqa: ANN001
        return (
            ProductListItemDtoPagedResponse(
                items=[
                    ProductListItemDto(
                        id="prod-alto",
                        name="Alto Chair",
                        departmentId=3,
                        subDepartmentId=None,
                        categoryId="cat-chair",
                        isActive=True,
                        variations=[
                            ProductVariationDto(
                                id="var-colour",
                                name="Colour",
                                options=[
                                    ProductVariationOptionDto(id="opt-black", name="Black"),
                                    ProductVariationOptionDto(id="opt-white", name="White"),
                                ],
                            )
                        ],
                        variants=[
                            ProductVariantDto(
                                id="var-alto-black",
                                name="Alto Chair - Black",
                                sku="fn-se-ch-alt-bla",
                                totalHirable=172,
                                optionIds=["opt-black"],
                            ),
                            ProductVariantDto(
                                id="var-alto-white",
                                name="Alto Chair - White",
                                sku="fn-se-ch-alt-whi",
                                totalHirable=232,
                                optionIds=["opt-white"],
                            ),
                        ],
                    )
                ],
                page=1,
                pageSize=50,
                totalCount=1,
                totalPages=1,
            ),
            "memory_hit",
            [],
        )

    monkeypatch.setattr("app.tool.stock.service.InventoryService.search_catalogue", fake_search_catalogue)

    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "resolver.disambiguate_candidates",
                "args": {"query": "alto chair", "limit": 10},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "resolver.disambiguate_candidates"
    assert payload["data"]["status"] == "resolved_product_family"
    assert payload["data"]["variant_count"] >= 1
    assert payload["data"]["product_id"]


def test_currency_history_rebases_when_primary_base_is_restricted(monkeypatch) -> None:
    async def fake_primary(self, path, params):  # noqa: ANN001
        if path == "/2026-04-22" and params.get("base") == "USD":
            raise CurrencyProviderError(
                provider="exchangeratesapi",
                status_code=400,
                message="Base currency access restricted.",
                code="base_currency_access_restricted",
            )
        if path == "/2026-04-22":
            return {
                "success": True,
                "historical": True,
                "date": "2026-04-22",
                "base": "EUR",
                "rates": {"USD": 0.65, "AUD": 1.0},
            }
        raise AssertionError(f"Unexpected primary request: path={path} params={params}")

    async def fail_fallback(self, path, params):  # noqa: ANN001
        raise AssertionError(f"Fallback should not be used: path={path} params={params}")

    monkeypatch.setattr("app.tool.currency.service.CurrencyService._request_primary_json", fake_primary)
    monkeypatch.setattr("app.tool.currency.service.CurrencyService._request_fallback_json", fail_fallback)

    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "currency.history",
                "args": {"date": "2026-04-22", "base": "USD", "symbols": "AUD"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "currency.history"
    assert payload["data"]["base"] == "USD"
    assert payload["data"]["provider"] == "exchangeratesapi_cross_rate"
    assert payload["data"]["rates"]["AUD"] == 1.0 / 0.65


def test_currency_convert_falls_back_when_primary_provider_is_unavailable(monkeypatch) -> None:
    async def fake_primary(self, path, params):  # noqa: ANN001
        raise CurrencyProviderError(
            provider="exchangeratesapi",
            status_code=503,
            message="EXCHANGE_RATE_API is not configured in the environment.",
            code="missing_api_key",
        )

    async def fake_fallback(self, path, params):  # noqa: ANN001
        assert path == "/2025-04-22"
        assert params == {"base": "AUD", "symbols": "USD"}
        return {"amount": 1.0, "base": "AUD", "date": "2025-04-22", "rates": {"USD": 0.66}}

    monkeypatch.setattr("app.tool.currency.service.CurrencyService._request_primary_json", fake_primary)
    monkeypatch.setattr("app.tool.currency.service.CurrencyService._request_fallback_json", fake_fallback)

    with build_client() as client:
        response = client.post(
            "/api/v1/tools/call",
            json={
                "tool": "currency.convert",
                "args": {"from": "aud", "to": "usd", "amount": 250, "date": "2025-04-22"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "currency.convert"
    assert payload["data"]["query"] == {"from": "AUD", "to": "USD", "amount": 250.0}
    assert payload["data"]["info"]["rate"] == 0.66
    assert payload["data"]["result"] == 165.0
    assert payload["data"]["provider"] == "frankfurter"


def test_query_endpoint_uses_agent_engine_and_exposes_ui_entrypoint(monkeypatch) -> None:
    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved Laminate Timber Floor from the local Harmonise simulator.",
            thoughts=["<thought>goal: resolve sku</thought>"],
            tool_trace=[
                ToolTrace(
                    thought="<thought>goal: resolve sku</thought>",
                    tool="stock.get_product",
                    args={"sku": "fl-la-la-lam-1-ble"},
                    status="ok",
                    cache_status="memory_hit",
                    source_data="harmonise -> products.items[*]",
                    result_count=1,
                )
            ],
            limitations=[],
            resolved_items=[],
            plan_status=PlanStatus(
                goal="Resolve SKU",
                steps=[
                    PlanStep(
                        id=1,
                        name="fetch product",
                        tool="stock.get_product",
                        status="done",
                        args={"sku": "fl-la-la-lam-1-ble"},
                        hypotheses=[],
                        validation=None,
                    )
                ],
                memo=MemoCache(entries=[], aggregates={}),
                status="complete",
            ),
        )

    monkeypatch.setattr(AgentEngine, "run", fake_run)

    with build_client() as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Check fl-la-la-lam-1-ble", "renderMockUi": True},
        )

        ui_response = client.get("/api/v1/ui")
        logo_response = client.get("/api/v1/ui/public/hth.jpeg")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "answered"
    assert payload["plan_status"]["status"] == "complete"
    assert payload["plan_status"]["steps"][0]["tool"] == "stock.get_product"
    assert payload["mock_ui"] is None
    assert payload["mock_ui_path"] == "/api/v1/ui"
    assert ui_response.status_code == 200
    assert "HTH Claude" in ui_response.text
    assert "/api/v1/ui/assets/app.js" in ui_response.text
    assert "/api/v1/ui/public/hth.jpeg" in ui_response.text
    assert '"logoUrl": "/api/v1/ui/public/hth.jpeg"' in ui_response.text
    assert logo_response.status_code == 200
    assert logo_response.headers["content-type"] == "image/jpeg"


def test_query_endpoint_returns_structured_debug_payload(monkeypatch) -> None:
    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved Laminate Timber Floor from the local Harmonise simulator.",
            thoughts=["<thought>goal: resolve sku</thought>"],
            debug=AgentDebugPayload(
                intent=AgentDebugIntent(
                    current_goal="Resolve Laminate Timber Floor",
                    primary_entity_guess="variant",
                    requested_attributes=["size", "stock"],
                    inferred_filters={"sku": "fl-la-la-lam-1-ble"},
                    scope_status="stock_supported",
                ),
                plan=AgentDebugPlan(
                    goal="Resolve Laminate Timber Floor",
                    status="complete",
                    ready_steps=[],
                    blocked_steps=[],
                    dag=[
                        AgentDebugPlanStep(
                            id=1,
                            name="fetch product",
                            tool="stock.get_product",
                            status="done",
                            depends_on=[],
                            parallel_group=None,
                        )
                    ],
                    next_hop_rules=["Follow catalogue matches with exact product detail retrieval."],
                ),
                retrieval=AgentDebugRetrieval(
                    thought_blocks=["<thought>goal: resolve sku</thought>"],
                ),
                grounding=AgentDebugGrounding(
                    resolved_identifiers=["fl-la-la-lam-1-ble"],
                    evidence_count=1,
                    unresolved_attributes=[],
                    user_impact_limitations=[],
                ),
            ),
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    monkeypatch.setattr(AgentEngine, "run", fake_run)

    with build_client() as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Check fl-la-la-lam-1-ble", "includeThoughts": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["debug"]["intent"]["primary_entity_guess"] == "variant"
    assert payload["debug"]["plan"]["dag"][0]["tool"] == "stock.get_product"
    assert payload["debug"]["grounding"]["evidence_count"] == 1


def test_query_endpoint_hides_debug_payload_when_include_thoughts_is_false(monkeypatch) -> None:
    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=["<thought>goal: resolve sku</thought>"],
            debug=AgentDebugPayload(
                intent=AgentDebugIntent(current_goal="Resolve request"),
                plan=AgentDebugPlan(goal="Resolve request", status="complete"),
                retrieval=AgentDebugRetrieval(thought_blocks=["<thought>goal: resolve sku</thought>"]),
                grounding=AgentDebugGrounding(evidence_count=0),
            ),
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    monkeypatch.setattr(AgentEngine, "run", fake_run)

    with build_client() as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Check fl-la-la-lam-1-ble", "includeThoughts": False},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["thoughts"] == []
    assert payload["debug"] is None


def test_system_spec_reports_harmonise_orchestrator_scope() -> None:
    with build_client() as client:
        response = client.get("/api/v1/system/spec")

    assert response.status_code == 200
    payload = response.json()
    assert payload["persona"] == "Harmonise Orchestrator"
    assert payload["logo_url"] == "/api/v1/ui/public/hth.jpeg"
    assert payload["integration_surfaces"]["mcp"]["logo_url"] == "/api/v1/ui/public/hth.jpeg"
    assert "separate audit/debug payloads for planner and retrieval traces" in payload["scope"]
    assert "not yet implemented in the current tool contract" in payload["out_of_scope"][0]


def test_mock_ui_asset_prefers_structured_debug_payload_when_present() -> None:
    asset = Path("/Users/liamle/Downloads/hth-mcp/ui/mock/assets/app.js").read_text()

    assert "payload?.debug" in asset
    assert "buildRuntimeDebug(payload)" in asset
    assert "avatar-logo" in asset


def test_query_endpoint_replays_local_chat_history_across_turns(monkeypatch) -> None:
    seen_history: list[list[dict[str, str]]] = []

    async def fake_run(self, request, session_state):  # noqa: ANN001
        seen_history.append([turn.model_dump(mode="json") for turn in session_state.conversation_history])
        return AgentRun(
            status="answered",
            answer=f"Echo: {request.message}",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    monkeypatch.setattr(AgentEngine, "run", fake_run)

    session_id = f"local-chat-{uuid4()}"
    with build_client() as client:
        first = client.post(
            "/api/v1/query",
            json={"message": "First question", "sessionId": session_id},
        )
        second = client.post(
            "/api/v1/query",
            json={"message": "Follow-up question", "sessionId": session_id},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert seen_history[0] == []
    assert seen_history[1] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "Echo: First question"},
    ]


def test_query_endpoint_persists_session_evidence_for_pronoun_follow_up(monkeypatch) -> None:
    seen_state: list[dict[str, object]] = []

    async def fake_run(self, request, session_state):  # noqa: ANN001
        seen_state.append(
            {
                "message": request.message,
                "recent_product_names": list(session_state.recent_product_names),
                "recent_resolved_identifiers": list(session_state.recent_resolved_identifiers),
            }
        )
        if request.message != "them":
            session_state.recent_product_names = ["Alto Chair", *session_state.recent_product_names]
            session_state.recent_resolved_identifiers = [
                "prod-alto",
                "fn-se-ch-alt-bla",
                *session_state.recent_resolved_identifiers,
            ]
        return AgentRun(
            status="answered",
            answer=f"Echo: {request.message}",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    monkeypatch.setattr(AgentEngine, "run", fake_run)

    session_id = f"pronoun-follow-up-{uuid4()}"
    with build_client() as client:
        first = client.post(
            "/api/v1/query",
            json={"message": "Tell me about Alto Chair", "sessionId": session_id},
        )
        second = client.post(
            "/api/v1/query",
            json={"message": "them", "sessionId": session_id},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert seen_state[0]["recent_product_names"] == []
    assert seen_state[1]["recent_product_names"] == ["Alto Chair"]
    assert seen_state[1]["recent_resolved_identifiers"] == ["prod-alto", "fn-se-ch-alt-bla"]


def test_query_endpoint_can_disable_local_chat_history(monkeypatch) -> None:
    seen_history: list[list[dict[str, str]]] = []

    async def fake_run(self, request, session_state):  # noqa: ANN001
        seen_history.append([turn.model_dump(mode="json") for turn in session_state.conversation_history])
        return AgentRun(
            status="answered",
            answer=f"Echo: {request.message}",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    monkeypatch.setattr(AgentEngine, "run", fake_run)

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        local_chat_memory_enabled=False,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
    )

    session_id = f"no-local-chat-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/api/v1/query",
            json={"message": "First question", "sessionId": session_id},
        )
        second = client.post(
            "/api/v1/query",
            json={"message": "Follow-up question", "sessionId": session_id},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert seen_history[0] == []
    assert seen_history[1] == []


def test_query_endpoint_uses_fallback_name_when_llm_naming_fails(monkeypatch) -> None:
    calls = {"naming": 0}

    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    async def fake_complete_with_model(self, model, messages, max_completion_tokens=40):  # noqa: ANN001
        calls["naming"] += 1
        if calls["naming"] == 1:
            raise UpstreamServiceError(504, "Transient SLM timeout.")
        return {"choices": [{"message": {"content": "Expo Floor Plan"}}]}

    monkeypatch.setattr(AgentEngine, "run", fake_run)
    monkeypatch.setattr(AgentEngine, "complete_with_model", fake_complete_with_model)

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
        foundry_slm_model="gpt-5.4-mini",
    )

    session_id = f"naming-retry-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["session_state"]["name_assigned"] is True
    assert first_payload["session_state"]["session_name"] == "Need a laminate quote"
    assert first_payload["session_state"]["session_name_source"] == "fallback"
    assert calls["naming"] == 1


def test_query_endpoint_does_not_retry_naming_after_fallback(monkeypatch) -> None:
    calls = {"naming": 0}

    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    async def fake_complete_with_model(self, model, messages, max_completion_tokens=40):  # noqa: ANN001
        calls["naming"] += 1
        if calls["naming"] == 1:
            raise UpstreamServiceError(504, "Transient SLM timeout.")
        return {"choices": [{"message": {"content": "Expo Floor Plan"}}]}

    monkeypatch.setattr(AgentEngine, "run", fake_run)
    monkeypatch.setattr(AgentEngine, "complete_with_model", fake_complete_with_model)

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
        foundry_slm_model=None,
    )

    session_id = f"naming-retry-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )
        second = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )
        third = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["session_state"]["session_name"] == "Need a laminate quote"
    assert first_payload["session_state"]["session_name_source"] == "fallback"

    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["session_state"]["session_name"] == "Need a laminate quote"
    assert second_payload["session_state"]["session_name_source"] == "fallback"

    assert third.status_code == 200
    third_payload = third.json()
    assert third_payload["session_state"]["session_name"] == "Need a laminate quote"
    assert third_payload["session_state"]["session_name_source"] == "fallback"

    assert calls["naming"] == 1


def test_query_endpoint_names_using_primary_model_when_slm_missing(monkeypatch) -> None:
    calls = {"naming": 0}

    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
        foundry_slm_model=None,
    )
    expected_model = settings.foundry_model

    async def fake_complete_with_model(self, model, messages, max_completion_tokens=40):  # noqa: ANN001
        calls["naming"] += 1
        assert model == expected_model
        return {"choices": [{"message": {"content": "Global price check"}}]}

    monkeypatch.setattr(AgentEngine, "run", fake_run)
    monkeypatch.setattr(AgentEngine, "complete_with_model", fake_complete_with_model)

    session_id = f"naming-primary-model-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_state"]["name_assigned"] is True
    assert payload["session_state"]["session_name"] == "Global price check"
    assert payload["session_state"]["session_name_source"] == "llm"
    assert calls["naming"] == 1


def test_query_endpoint_logs_too_short_session_name_output(monkeypatch, capsys) -> None:  # noqa: ANN001
    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    async def fake_complete_with_model(self, model, messages, max_completion_tokens=40):  # noqa: ANN001
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Inventory"}}]}

    monkeypatch.setattr(AgentEngine, "run", fake_run)
    monkeypatch.setattr(AgentEngine, "complete_with_model", fake_complete_with_model)

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
    )

    session_id = f"naming-short-output-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_state"]["session_name"] == "Need a laminate quote"
    assert payload["session_state"]["session_name_source"] == "fallback"
    captured = capsys.readouterr()
    assert "Session naming fallback applied (too short llm output)" in captured.err
    assert "raw_title=Inventory" in captured.err


def test_query_endpoint_retries_truncated_session_name_output(monkeypatch) -> None:
    calls = {"naming": 0, "tokens": []}

    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    async def fake_complete_with_model(self, model, messages, max_completion_tokens=40):  # noqa: ANN001
        calls["naming"] += 1
        calls["tokens"].append(max_completion_tokens)
        if calls["naming"] == 1:
            return {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Expo Floor Plan"}}]}

    monkeypatch.setattr(AgentEngine, "run", fake_run)
    monkeypatch.setattr(AgentEngine, "complete_with_model", fake_complete_with_model)

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
    )

    session_id = f"naming-truncated-output-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_state"]["session_name"] == "Expo Floor Plan"
    assert payload["session_state"]["session_name_source"] == "llm"
    assert calls["naming"] == 2
    assert calls["tokens"] == [40, 80]


def test_query_endpoint_uses_smart_fallback_for_lead_in_message(monkeypatch) -> None:
    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    async def fake_complete_with_model(self, model, messages, max_completion_tokens=40):  # noqa: ANN001
        return {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}

    monkeypatch.setattr(AgentEngine, "run", fake_run)
    monkeypatch.setattr(AgentEngine, "complete_with_model", fake_complete_with_model)

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
    )

    session_id = f"naming-smart-fallback-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Let me know the raw title", "sessionId": session_id},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_state"]["session_name"] == "raw title"
    assert payload["session_state"]["session_name_source"] == "fallback"


def test_query_endpoint_accepts_structured_session_name_content(monkeypatch) -> None:
    async def fake_run(self, request, session_state):  # noqa: ANN001
        return AgentRun(
            status="answered",
            answer="Resolved request.",
            thoughts=[],
            tool_trace=[],
            limitations=[],
            resolved_items=[],
        )

    async def fake_complete_with_model(self, model, messages, max_completion_tokens=40):  # noqa: ANN001
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": [
                            {"type": "output_text", "text": "Expo"},
                            {"type": "output_text", "text": "Floor Plan"},
                        ]
                    },
                }
            ]
        }

    monkeypatch.setattr(AgentEngine, "run", fake_run)
    monkeypatch.setattr(AgentEngine, "complete_with_model", fake_complete_with_model)

    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="test-key",
    )

    session_id = f"naming-structured-output-{uuid4()}"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_state"]["session_name"] == "Expo Floor Plan"
    assert payload["session_state"]["session_name_source"] == "llm"


def test_ui_route_returns_404_when_simulation_disabled() -> None:
    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        enable_mock_ui_simulation=False,
        redis_fallback_enabled=True,
        redis_url=TEST_REDIS_URL,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/ui")

    assert response.status_code == 404


def test_http_app_does_not_expose_fake_mcp_routes() -> None:
    with build_client() as client:
        get_response = client.get("/mcp")
        post_response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0.0"},
                },
            },
        )

    assert get_response.status_code != 401
    assert post_response.status_code != 401
