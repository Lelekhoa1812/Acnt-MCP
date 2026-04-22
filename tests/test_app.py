from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent.engine import AgentEngine, AgentRun
from app.currency.service import CurrencyProviderError
from app.config import Settings
from app.errors import UpstreamServiceError
from app.main import create_app
from app.schemas import ToolTrace


def build_client() -> TestClient:
    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        mock_catalog_path="./mock/product-catalog-enriched.json",
        mock_details_path="./mock/product-details-enriched.json",
        mock_departments_path="./mock/departments.json",
        mock_categories_path="./mock/categories.json",
        redis_fallback_enabled=True,
        enable_mock_ui_simulation=True,
        mock_ui_path="./ui/mock/index.html",
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


def test_tools_endpoint_lists_stock_and_plugin_tools() -> None:
    with build_client() as client:
        response = client.get("/api/v1/tools")

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["tools"]}
    assert "stock.search_catalogue" in tool_names
    assert "stock.inventory_snapshot" in tool_names
    assert "resolver.disambiguate_candidates" in tool_names
    assert "weather.current" in tool_names
    assert "news.search" in tool_names
    assert "currency.convert" in tool_names


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
    assert "total=2566" in row["stock"]
    assert "10m Hex Carpet Set - Onyx" in row["attributeEvidence"]
    assert any(spec.startswith("salesNote=") for spec in row["knownSpecs"])


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

    monkeypatch.setattr("app.currency.service.CurrencyService._request_primary_json", fake_primary)
    monkeypatch.setattr("app.currency.service.CurrencyService._request_fallback_json", fail_fallback)

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

    monkeypatch.setattr("app.currency.service.CurrencyService._request_primary_json", fake_primary)
    monkeypatch.setattr("app.currency.service.CurrencyService._request_fallback_json", fake_fallback)

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
        )

    monkeypatch.setattr(AgentEngine, "run", fake_run)

    with build_client() as client:
        response = client.post(
            "/api/v1/query",
            json={"message": "Check fl-la-la-lam-1-ble", "renderMockUi": True},
        )

        ui_response = client.get("/api/v1/ui")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "answered"
    assert payload["mock_ui"] is None
    assert payload["mock_ui_path"] == "/api/v1/ui"
    assert ui_response.status_code == 200
    assert "HTH Claude" in ui_response.text
    assert "/api/v1/ui/assets/app.js" in ui_response.text


def test_query_endpoint_retries_session_naming_after_transient_failure(monkeypatch) -> None:
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
        second = client.post(
            "/api/v1/query",
            json={"message": "Need a laminate quote", "sessionId": session_id},
        )

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["session_state"]["name_assigned"] is False

    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["session_state"]["session_name"] == "Expo Floor Plan"
    assert second_payload["session_state"]["name_assigned"] is True
    assert calls["naming"] == 2


def test_ui_route_returns_404_when_simulation_disabled() -> None:
    settings = Settings(
        local_harmonise=True,
        log_level="debug",
        enable_mock_ui_simulation=False,
        redis_fallback_enabled=True,
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

    assert get_response.status_code == 404
    assert post_response.status_code == 404
