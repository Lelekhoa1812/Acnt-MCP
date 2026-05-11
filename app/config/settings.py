from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    server_name: str = Field("hth-stock-intelligence", alias="HTH_SERVER_NAME")
    server_version: str = Field("1.0.5", alias="HTH_SERVER_VERSION")
    transport: str = Field("http", alias="HTH_TRANSPORT")
    # Motivation vs Logic: default to port 80 so the service aligns with standard HTTP ingress expectations.
    port: int = Field(80, alias="HTH_PORT")
    api_prefix: str = Field("/api/v1", alias="HTH_API_PREFIX")
    log_level: str = Field("debug", alias="HTH_LOG_LEVEL")
    data_source: str = Field("harmonise", alias="HTH_DATA_SOURCE")
    local_harmonise: bool = Field(False, alias="LOCAL_HARMONISE")
    local_harmonise_endpoint: str = Field("http://localhost:9000", alias="LOCAL_HARMONISE_ENDPOINT")
    cloud_harmonise_endpoint: str = Field("http://localhost:9000", alias="CLOUD_HARMONISE_ENDPOINT")
    cloud_harmonise_api: str | None = Field(None, alias="CLOUD_HARMONISE_API")
    cloud_harmonise_image: str | None = Field(None, alias="CLOUD_HARMONISE_IMAGE")
    harmonise_headers: str = Field("{}", alias="HTH_HARMONISE_HEADERS")
    harmonise_timeout_ms: int = Field(0, ge=0, alias="HTH_HARMONISE_TIMEOUT_MS")
    harmonise_retry_attempts: int = Field(2, ge=0, alias="HTH_HARMONISE_RETRY_ATTEMPTS")
    harmonise_retry_backoff_ms: int = Field(400, ge=0, alias="HTH_HARMONISE_RETRY_BACKOFF_MS")
    harmonise_retry_backoff_cap_ms: int = Field(2400, ge=0, alias="HTH_HARMONISE_RETRY_BACKOFF_CAP_MS")
    # Motivation vs Logic: the cloud /api/v1/products list route can 500 when many
    # in-flight GETs run at once (e.g. parallel stock_search_catalogue). This caps
    # concurrent requests per process to the same Harmonise base URL. Local ASGI
    # uses a high effective limit in HarmoniseInventorySource.
    harmonise_concurrent_request_limit: int = Field(2, ge=1, alias="HTH_HARMONISE_CONCURRENT_REQUESTS")
    redis_url: str = Field("redis://localhost:6379", alias="HTH_REDIS_URL")
    cache_ttl_seconds: int = Field(300, alias="HTH_CACHE_TTL_SECONDS")
    session_ttl_seconds: int = Field(1800, alias="HTH_SESSION_TTL_SECONDS")
    default_session_id: str = Field("default", alias="HTH_DEFAULT_SESSION_ID")
    mock_catalog_path: str = Field("./mock/product-catalog.json", alias="HTH_MOCK_CATALOG_PATH")
    mock_details_path: str = Field("./mock/product-details.json", alias="HTH_MOCK_DETAILS_PATH")
    mock_departments_path: str = Field("./mock/departments.json", alias="HTH_MOCK_DEPARTMENTS_PATH")
    mock_categories_path: str = Field("./mock/categories.json", alias="HTH_MOCK_CATEGORIES_PATH")
    enable_mock_ui_simulation: bool = Field(True, alias="HTH_ENABLE_MOCK_UI_SIMULATION")
    mock_ui_path: str = Field("./ui/mock/index.html", alias="HTH_MOCK_UI_PATH")
    # Motivation vs Logic: keep the UI and MCP connector branding driven from
    # one setting so clients that surface server icons stay visually aligned.
    server_logo_url: str | None = Field(None, alias="HTH_SERVER_LOGO_URL")
    server_website_url: str | None = Field(None, alias="HTH_SERVER_WEBSITE_URL")
    # Motivation vs Logic: default false so session/tool caches use Redis as the
    # durable source of truth; set HTH_REDIS_FALLBACK_ENABLED=true for dev without Redis.
    redis_fallback_enabled: bool = Field(False, alias="HTH_REDIS_FALLBACK_ENABLED")
    # Motivation vs Logic: local REST `/query` runs do not get Claude-managed
    # browser memory, so we keep a short device-local conversation buffer in
    # process memory (never Redis) to preserve follow-up context while developing.
    local_chat_memory_enabled: bool = Field(True, alias="HTH_LOCAL_CHAT_MEMORY_ENABLED")
    local_chat_memory_turns: int = Field(6, ge=1, alias="HTH_LOCAL_CHAT_MEMORY_TURNS")
    # Root Cause vs Logic: complex stock/variant requests often need more than
    # a handful of tool calls before the plan completes, so the default loop
    # budget should match the documented sample config instead of the previous
    # overly tight value that frequently triggered the max-step guard.
    agent_max_steps: int = Field(15, alias="HTH_AGENT_MAX_STEPS")
    # Motivation vs Logic: recursive detail follow-up should be policy-driven so
    # runtime fan-out is not hard-coded in engine logic.
    agent_recursive_follow_up_max_products: int = Field(
        20,
        ge=1,
        alias="HTH_AGENT_RECURSIVE_FOLLOW_UP_MAX_PRODUCTS",
    )
    # Motivation vs Logic: autonomous recovery should retry missing evidence in
    # bounded rounds before returning a limited answer.
    agent_replan_max_rounds: int = Field(2, ge=0, alias="HTH_AGENT_REPLAN_MAX_ROUNDS")
    agent_replan_max_steps_per_round: int = Field(
        10,
        ge=1,
        alias="HTH_AGENT_REPLAN_MAX_STEPS_PER_ROUND",
    )
    # Motivation vs Logic: runtime follow-up product detail page sizing should
    # remain configurable instead of fixed in planner glue code.
    agent_get_product_page_size: int = Field(100, ge=1, le=100, alias="HTH_AGENT_GET_PRODUCT_PAGE_SIZE")
    # Motivation vs Logic: one-request-many-items should split into bounded,
    # single-item search calls so decomposition stays explicit and controllable.
    agent_search_split_max_items: int = Field(10, ge=1, alias="HTH_AGENT_SEARCH_SPLIT_MAX_ITEMS")
    # Motivation vs Logic: keep variant resolution fast by allowing more concurrent
    # Harmonise detail lookups; any extra items automatically wait on the semaphore.
    stock_parallel_requests_limit: int = Field(50, alias="HTH_STOCK_PARALLEL_REQUESTS_LIMIT")
    # Motivation vs Logic: variant-rich product families can make full spec hydration
    # slow and prompt-heavy, so cap family enrichment while preserving exact lookups.
    max_cap_variant: int = Field(20, ge=1, alias="MAX_CAP_VARIANT")
    # Motivation vs Logic: inventory_snapshot fans out one GET per catalogue row; the
    # same cloud endpoint can return 500 under high concurrency even when each URL
    # works from Postman. This caps detail fan-out separately from compare_variants.
    stock_snapshot_detail_parallel_limit: int = Field(8, ge=1, alias="HTH_STOCK_SNAPSHOT_DETAIL_PARALLEL")
    # Motivation vs Logic: snapshot expansion should be configurable so broadening
    # behavior remains policy-driven instead of hidden magic constants.
    snapshot_expand_max_initial_items: int = Field(3, ge=1, alias="HTH_SNAPSHOT_EXPAND_MAX_INITIAL_ITEMS")
    snapshot_specificity_threshold: float = Field(0.78, ge=0.0, le=1.0, alias="HTH_SNAPSHOT_SPECIFICITY_THRESHOLD")
    snapshot_expand_parallel_pages_limit: int = Field(8, ge=1, alias="HTH_SNAPSHOT_EXPAND_PARALLEL_PAGES_LIMIT")
    # Motivation vs Logic: snapshot catalogue scans (initial inventory_snapshot list pass
    # and category expansion) must not crawl unbounded pages when Harmonise reports a
    # large totalPages count; same knob caps both paths (env name kept for compatibility).
    snapshot_expand_max_department_pages: int = Field(10, ge=1, alias="HTH_SNAPSHOT_EXPAND_MAX_DEPARTMENT_PAGES")
    # Test-only: appended to snapshot department-expansion catalogue cache keys so
    # parallel pytest cases do not reuse Redis rows from unrelated Harmonise mocks.
    inventory_test_catalogue_cache_scope: str | None = Field(
        default=None,
        alias="HTH_INVENTORY_TEST_CATALOGUE_CACHE_SCOPE",
    )
    # Motivation vs Logic: broad inventory answers can legitimately need large
    # Markdown tables, so the agent completion budget is configurable instead of
    # hard-coded to a small single-paragraph default.
    agent_completion_tokens: int = Field(3600, alias="HTH_AGENT_COMPLETION_TOKENS")
    foundry_endpoint: str | None = Field(None, alias="AZURE_AI_FOUNDRY_ENDPOINT")
    foundry_api_key: str | None = Field(None, alias="AZURE_AI_FOUNDRY_API_KEY")
    foundry_model: str = Field("gpt-5.4-mini", alias="AZURE_AI_FOUNDRY_MODEL")
    foundry_slm_model: str | None = Field(None, alias="AZURE_AI_FOUNDRY_SLM")
    foundry_timeout_ms: int = Field(60000, alias="AZURE_AI_FOUNDRY_TIMEOUT_MS")
    # Motivation vs Logic: read timeouts from Azure are usually transient, so we
    # retry a few times before surfacing a 5xx to callers.
    foundry_retry_attempts: int = Field(2, ge=0, alias="AZURE_AI_FOUNDRY_RETRY_ATTEMPTS")
    foundry_retry_backoff_ms: int = Field(600, ge=0, alias="AZURE_AI_FOUNDRY_RETRY_BACKOFF_MS")
    foundry_retry_backoff_cap_ms: int = Field(4000, ge=0, alias="AZURE_AI_FOUNDRY_RETRY_BACKOFF_CAP_MS")
    exchange_rate_api_key: str | None = Field(None, alias="EXCHANGE_RATE_API")
    open_weather_api_key: str | None = Field(None, alias="OPEN_WEATHER_API")
    news_api_key: str | None = Field(None, alias="NEWS_API")
    ebay_client_id: str | None = Field(None, alias="EBAY_CLIENT_ID")
    ebay_client_secret: str | None = Field(None, alias="EBAY_CLIENT_SECRET")
    ebay_marketplace_id: str = Field("EBAY_US", alias="EBAY_MARKETPLACE_ID")
    ebay_environment: str = Field("production", alias="EBAY_ENVIRONMENT")
    # Open Collective powers the accounting tools. API-only, no self-hosting required.
    # Completely free (MIT licensed, open-source). Get PAT token from:
    # https://opencollective.com/dashboard/settings/applications
    opencollective_graphql_url: str = Field("https://api.opencollective.com/graphql/v2", alias="OPENCOLLECTIVE_GRAPHQL_URL")
    opencollective_pat_token: str | None = Field(None, alias="OPENCOLLECTIVE_PAT_TOKEN")
    opencollective_client_id: str | None = Field(None, alias="OPENCOLLECTIVE_CLIENT_ID")
    opencollective_client_secret: str | None = Field(None, alias="OPENCOLLECTIVE_CLIENT_SECRET")
    public_base_url: str | None = Field(None, alias="HTH_PUBLIC_BASE_URL")
    mcp_path: str = Field("/mcp", alias="HTH_MCP_PATH")
    mcp_stateless: bool = Field(False, alias="HTH_MCP_STATELESS")
    mcp_json_response: bool = Field(False, alias="HTH_MCP_JSON_RESPONSE")
    mcp_retry_interval_ms: int | None = Field(2500, ge=0, alias="HTH_MCP_RETRY_INTERVAL_MS")
    mcp_session_idle_timeout_seconds: float | None = Field(
        1800.0,
        ge=0,
        alias="HTH_MCP_SESSION_IDLE_TIMEOUT_SECONDS",
    )
    # Root Cause vs Logic: hosted MCP deployments are easy to misconfigure with
    # a single-digit idle timeout, which causes normal connector polling gaps to
    # reap sessions and force repeated initialize/list-tools cycles. Keep a
    # configurable floor for stateful HTTP sessions while still allowing `0` to
    # disable idle reaping completely.
    mcp_session_idle_timeout_min_seconds: float = Field(
        1800.0,
        ge=1,
        alias="HTH_MCP_SESSION_IDLE_TIMEOUT_MIN_SECONDS",
    )
    mcp_bearer_token: str | None = Field(None, alias="HTH_MCP_BEARER_TOKEN")
    # Motivation vs Logic: remote Claude web connectors can complete OAuth, but
    # many hosted connector UIs do not let you pre-supply a bearer token. Keep
    # bearer enforcement opt-in so the MCP endpoint can be public by default.
    mcp_require_bearer_token: bool = Field(False, alias="HTH_MCP_REQUIRE_BEARER_TOKEN")
    mcp_allowed_hosts: str | None = Field(None, alias="HTH_MCP_ALLOWED_HOSTS")
    mcp_allowed_origins: str | None = Field(None, alias="HTH_MCP_ALLOWED_ORIGINS")
    mcp_oauth_token_ttl_seconds: int = Field(3600, ge=60, alias="HTH_MCP_OAUTH_TOKEN_TTL_SECONDS")
    # Motivation vs Logic: the OAuth bridge should issue a real JWT access token
    # so the same server can validate `/mcp` requests without a second auth path.
    # Prefer an explicit signing secret in production, but keep the bearer token as
    # a backward-compatible fallback for existing deployments.
    mcp_oauth_jwt_secret: str | None = Field(None, alias="HTH_MCP_OAUTH_JWT_SECRET")
    mcp_oauth_auto_trusted_redirect_domains: str | None = Field(
        "chatgpt.com,openai.com,claude.ai,claude.com,mistral.ai",
        alias="AUTO_TRUSTED_DOMAINS",
    )
    identity_auth_enabled: bool = Field(False, alias="HTH_IDENTITY_AUTH_ENABLED")
    auth_issuer: str | None = Field(None, alias="HTH_AUTH_ISSUER")
    auth_audience: str | None = Field(None, alias="HTH_AUTH_AUDIENCE")
    auth_jwks_url: str | None = Field(None, alias="HTH_AUTH_JWKS_URL")
    auth_jwt_hs256_secret: str | None = Field(None, alias="HTH_AUTH_JWT_HS256_SECRET")
    oauth_user_group: str = Field("SG-HTH-MCP-Users", alias="OAUTH_USER_GROUP")
    news_pl_group: str = Field("all", alias="NEWS_PL_GROUP")
    weather_pl_group: str = Field("all", alias="WEATHER_PL_GROUP")
    currency_pl_group: str = Field("all", alias="CURRENCY_PL_GROUP")
    stock_pl_group: str = Field("all", alias="STOCK_PL_GROUP")
    auth_required_claims: str = Field("tid,oid", alias="HTH_AUTH_REQUIRED_CLAIMS")
    # Department-based access is disabled for now; keep the setting commented
    # out so we can re-enable it later without reintroducing the old policy.
    # auth_department_claims: str = Field(
    #     "extension_departmentId,extension_department,officeLocation",
    #     alias="HTH_AUTH_DEPARTMENT_CLAIMS",
    # )
    auth_required_token_version: str | None = Field("2.0", alias="HTH_AUTH_REQUIRED_TOKEN_VERSION")
    auth_rate_limit_per_minute: int = Field(50, ge=0, alias="HTH_AUTH_RATE_LIMIT_PER_MINUTE")
    auth_group_cache_ttl_seconds: int = Field(300, ge=0, alias="HTH_AUTH_GROUP_CACHE_TTL_SECONDS")
    oauth_client_id: str | None = Field(None, alias="OAUTH_CLIENT_ID")
    oauth_client_secret: str | None = Field(None, alias="OAUTH_CLIENT_SECRET")
    oauth_client_auth_method: str | None = Field(None, alias="OAUTH_CLIENT_AUTH_METHOD")
    oauth_tenant_id: str | None = Field(None, alias="OAUTH_TENANT_ID")
    oauth_authority: str | None = Field(None, alias="OAUTH_AUTHORITY")
    oauth_audience: str | None = Field(None, alias="OAUTH_AUDIENCE")
    oauth_scope: str | None = Field(None, alias="OAUTH_SCOPE")
    oauth_graph_scopes: str = Field("User.Read Group.Read.All", alias="OAUTH_GRAPH_SCOPES")
    oauth_redirect_uri: str | None = Field(None, alias="OAUTH_REDIRECT_URI")

    @property
    def harmonise_timeout_seconds(self) -> float | None:
        # Motivation vs Logic: large cloud catalogue scans can legitimately take
        # much longer than a fixed client timeout, so `0` disables the HTTP read
        # timeout entirely and lets inventory retrieval run until upstream returns.
        if self.harmonise_timeout_ms == 0:
            return None
        return self.harmonise_timeout_ms / 1000

    @property
    def harmonise_retry_backoff_seconds(self) -> float:
        return self.harmonise_retry_backoff_ms / 1000

    @property
    def harmonise_retry_backoff_cap_seconds(self) -> float:
        return self.harmonise_retry_backoff_cap_ms / 1000

    @property
    def harmonise_max_attempts(self) -> int:
        return self.harmonise_retry_attempts + 1

    @property
    def foundry_timeout_seconds(self) -> float:
        return self.foundry_timeout_ms / 1000

    @property
    def foundry_retry_backoff_seconds(self) -> float:
        return self.foundry_retry_backoff_ms / 1000

    @property
    def foundry_retry_backoff_cap_seconds(self) -> float:
        return self.foundry_retry_backoff_cap_ms / 1000

    @property
    def foundry_max_attempts(self) -> int:
        return self.foundry_retry_attempts + 1

    @property
    def project_root(self) -> Path:
        return Path.cwd()

    @property
    def harmonise_mode(self) -> str:
        return "local" if self.local_harmonise else "remote"

    @property
    def harmonise_inventory_tools_enabled(self) -> bool:
        return self.data_source.strip().casefold() == "harmonise"

    @property
    def harmonise_base_url(self) -> str:
        return self.local_harmonise_endpoint if self.local_harmonise else self.cloud_harmonise_endpoint

    @property
    def data_source_label(self) -> str:
        if not self.harmonise_inventory_tools_enabled:
            return "disabled"
        return f"harmonise_{self.harmonise_mode}"

    @property
    def default_logo_url(self) -> str:
        # Motivation vs Logic: remote Claude web connectors treat relative
        # server metadata URLs as suspicious, so the default implementation URL
        # should resolve to an absolute HTTPS origin when public_base_url is set.
        if self.public_base_url:
            return f"{self.public_base_url.rstrip('/')}{self.api_prefix}/chat/public/hth.jpeg"
        return f"{self.api_prefix}/chat/public/hth.jpeg"

    @property
    def resolved_server_logo_url(self) -> str:
        url = self.server_logo_url or self.default_logo_url
        if self.public_base_url and url.startswith("/"):
            return f"{self.public_base_url.rstrip('/')}{url}"
        return url

    @property
    def default_website_url(self) -> str:
        if self.public_base_url:
            return f"{self.public_base_url.rstrip('/')}{self.api_prefix}/chat"
        return f"{self.api_prefix}/chat"

    @property
    def resolved_server_website_url(self) -> str:
        url = self.server_website_url or self.default_website_url
        if self.public_base_url and url.startswith("/"):
            return f"{self.public_base_url.rstrip('/')}{url}"
        return url

    @property
    def has_foundry(self) -> bool:
        return bool(self.foundry_endpoint and self.foundry_api_key)

    @property
    def has_slm_model(self) -> bool:
        return bool(self.foundry_slm_model)

    @property
    def harmonise_header_map(self) -> dict[str, str]:
        try:
            parsed: Any = json.loads(self.harmonise_headers)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(key): str(value) for key, value in parsed.items()}

    @property
    def harmonise_client_headers(self) -> dict[str, str]:
        # Motivation vs Logic: cloud Harmonise uses a fixed API-key contract,
        # while local simulation should avoid requiring cloud credentials.
        headers = dict(self.harmonise_header_map)
        if not self.local_harmonise and self.cloud_harmonise_api:
            headers["x-product-api-key"] = self.cloud_harmonise_api
        return headers

    def resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    def startup_notes(self) -> list[str]:
        notes: list[str] = []
        if not (self.project_root / ".env").exists():
            notes.append("No .env file detected; runtime settings are using environment variables and documented defaults.")
        if not self.has_foundry:
            notes.append(
                "Azure AI Foundry credentials are not fully configured; `/api/v1/query` requires "
                "AZURE_AI_FOUNDRY_ENDPOINT and AZURE_AI_FOUNDRY_API_KEY."
            )
        if self.local_harmonise:
            notes.append(
                "LOCAL_HARMONISE=true; the service runtimes will call the in-process Harmonise simulator "
                f"using LOCAL_HARMONISE_ENDPOINT ({self.local_harmonise_endpoint})."
            )
        if not self.harmonise_inventory_tools_enabled:
            notes.append(
                "HTH_DATA_SOURCE is not set to 'harmonise'; Harmonise-backed stock, resolver, and session tools "
                "are disabled for this run."
            )
        else:
            if not self.cloud_harmonise_endpoint:
                notes.append("CLOUD_HARMONISE_ENDPOINT is missing while LOCAL_HARMONISE=false.")
            if not self.cloud_harmonise_api:
                notes.append("CLOUD_HARMONISE_API is missing while LOCAL_HARMONISE=false.")
        if not self.redis_fallback_enabled:
            notes.append(
                "HTH_REDIS_FALLBACK_ENABLED=false; session and shared caches require a reachable Redis at "
                f"HTH_REDIS_URL ({self.redis_url}). Startup fails if Redis is down."
            )
        if self.local_chat_memory_enabled:
            notes.append(
                "HTH_LOCAL_CHAT_MEMORY_ENABLED=true; local `/api/v1/query` calls retain a short in-process "
                f"conversation buffer ({self.local_chat_memory_turns} turns, device-local only, non-Redis)."
            )
        effective_mcp_idle_timeout = self.effective_mcp_session_idle_timeout_seconds
        if (
            self.mcp_session_idle_timeout_seconds
            and effective_mcp_idle_timeout is not None
            and effective_mcp_idle_timeout != self.mcp_session_idle_timeout_seconds
        ):
            notes.append(
                "HTH_MCP_SESSION_IDLE_TIMEOUT_SECONDS is below the stateful MCP safety floor; using "
                f"{effective_mcp_idle_timeout:g}s instead of {self.mcp_session_idle_timeout_seconds:g}s."
            )
        if not self.mcp_bearer_token:
            notes.append(
                "HTH_MCP_BEARER_TOKEN is not configured; the public `/mcp` transport will accept unauthenticated requests."
            )
        elif self.mcp_require_bearer_token:
            notes.append(
                "HTH_MCP_REQUIRE_BEARER_TOKEN=true while HTH_MCP_BEARER_TOKEN is set; browser OAuth is enabled automatically from the bearer token."
            )
        if self.identity_auth_enabled and not (self.auth_jwks_url or self.auth_jwt_hs256_secret):
            notes.append(
                "HTH_IDENTITY_AUTH_ENABLED=true but no JWT verifier is configured. Set HTH_AUTH_JWKS_URL (Entra) "
                "or HTH_AUTH_JWT_HS256_SECRET (local test)."
            )
        if (
            self.oauth_client_id
            or self.oauth_client_secret
            or self.oauth_authority
            or self.oauth_tenant_id
            or self.auth_issuer
        ):
            if not self.oauth_client_id:
                notes.append(
                    "Claude OAuth login is partially configured. Set OAUTH_CLIENT_ID."
                )
            if not (self.oauth_authority or self.oauth_tenant_id or self.auth_issuer):
                notes.append(
                    "Claude OAuth login is missing OAUTH_AUTHORITY or OAUTH_TENANT_ID, so the login redirect "
                    "cannot be built."
                )
        return notes

    @staticmethod
    def _split_csv(raw: str | None) -> list[str]:
        if not raw:
            return []
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def parsed_mcp_allowed_hosts(self) -> list[str]:
        return self._split_csv(self.mcp_allowed_hosts)

    @property
    def parsed_mcp_allowed_origins(self) -> list[str]:
        configured = self._split_csv(self.mcp_allowed_origins)
        if not self.mcp_browser_oauth_enabled:
            return configured

        # Motivation vs Logic: browser MCP connectors need discovery and OAuth
        # responses to be readable cross-origin, so we automatically include the
        # known ChatGPT/Claude web origins unless the deployment overrides them.
        recommended_origins = [
            "https://chatgpt.com",
            "https://chat.openai.com",
            "https://claude.ai",
            "https://claude.com",
            "https://chat.mistral.ai",
        ]
        merged = list(configured)
        for origin in recommended_origins:
            if origin not in merged:
                merged.append(origin)
        return merged

    @property
    def parsed_mcp_oauth_auto_trusted_redirect_domains(self) -> list[str]:
        return [domain.lower() for domain in self._split_csv(self.mcp_oauth_auto_trusted_redirect_domains)]

    @property
    def effective_mcp_session_idle_timeout_seconds(self) -> float | None:
        if self.mcp_stateless:
            return None

        configured = self.mcp_session_idle_timeout_seconds
        if configured is None or configured == 0:
            return None

        retry_floor = 0.0
        if self.mcp_retry_interval_ms is not None:
            retry_floor = (self.mcp_retry_interval_ms / 1000) * 3
        return max(configured, self.mcp_session_idle_timeout_min_seconds, retry_floor)

    @property
    def parsed_auth_required_claims(self) -> list[str]:
        return self._split_csv(self.auth_required_claims)

    @property
    def parsed_oauth_user_groups(self) -> list[str]:
        return self._split_csv(self.oauth_user_group)

    @property
    def parsed_news_plugin_groups(self) -> list[str]:
        return self._split_csv(self.news_pl_group)

    @property
    def parsed_weather_plugin_groups(self) -> list[str]:
        return self._split_csv(self.weather_pl_group)

    @property
    def parsed_currency_plugin_groups(self) -> list[str]:
        return self._split_csv(self.currency_pl_group)

    @property
    def parsed_stock_plugin_groups(self) -> list[str]:
        return self._split_csv(self.stock_pl_group)

    def parsed_plugin_groups(self, plugin: str) -> list[str]:
        groups_by_plugin = {
            "news": self.parsed_news_plugin_groups,
            "weather": self.parsed_weather_plugin_groups,
            "currency": self.parsed_currency_plugin_groups,
            "stock": self.parsed_stock_plugin_groups,
        }
        return groups_by_plugin.get(plugin, [])

    # Department-based access is disabled for now, so there is no parser for
    # department claims in the active configuration path.

    @property
    def mcp_browser_oauth_enabled(self) -> bool:
        # Root Cause vs Logic: Claude.ai remote connectors need the OAuth bridge
        # even when the server is already protected by a bearer token. Treat the
        # bridge as enabled whenever a token exists so browser clients can redeem
        # it without requiring a second deployment toggle.
        return bool(self.mcp_bearer_token)

    @property
    def mcp_oauth_jwt_signing_secret(self) -> str | None:
        # Motivation vs Logic: keep legacy deployments working by falling back to
        # the configured bearer token, but allow an explicit secret when you want
        # the bridge-issued JWT to use a separate signing key.
        return self.mcp_oauth_jwt_secret or self.mcp_bearer_token

    @property
    def mcp_oauth_bridge_issuer(self) -> str | None:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        return None

    @property
    def claude_oauth_enabled(self) -> bool:
        return bool(
            self.oauth_client_id
            and (self.oauth_authority or self.oauth_tenant_id or self.auth_issuer)
        )

    @property
    def resolved_oauth_client_auth_method(self) -> str:
        configured = (self.oauth_client_auth_method or "").strip().casefold()
        if configured in {"none", "client_secret_post"}:
            return configured
        return "none"

    @property
    def parsed_oauth_graph_scopes(self) -> list[str]:
        return [part.strip() for part in self.oauth_graph_scopes.replace(",", " ").split() if part.strip()]

    @property
    def resolved_oauth_authority(self) -> str | None:
        if self.oauth_authority:
            return self.oauth_authority.rstrip("/")
        if self.oauth_tenant_id:
            return f"https://login.microsoftonline.com/{self.oauth_tenant_id.strip().strip('/')}"
        if self.auth_issuer:
            return self.auth_issuer.rstrip("/").removesuffix("/v2.0")
        return None

    @property
    def resolved_oauth_issuer(self) -> str | None:
        if self.oauth_authority:
            authority = self.oauth_authority.rstrip("/")
            if authority.endswith("/v2.0"):
                return authority
            return f"{authority}/v2.0"
        if self.auth_issuer:
            return self.auth_issuer.rstrip("/")
        authority = self.resolved_oauth_authority
        if not authority:
            return None
        if authority.endswith("/v2.0"):
            return authority
        return f"{authority}/v2.0"

    @property
    def resolved_oauth_authorize_url(self) -> str | None:
        authority = self.resolved_oauth_authority
        if not authority:
            return None
        return f"{authority}/oauth2/v2.0/authorize"

    @property
    def resolved_oauth_token_url(self) -> str | None:
        authority = self.resolved_oauth_authority
        if not authority:
            return None
        return f"{authority}/oauth2/v2.0/token"

    @property
    def resolved_ebay_base_url(self) -> str:
        if self.ebay_environment.strip().casefold() == "sandbox":
            return "https://api.sandbox.ebay.com"
        return "https://api.ebay.com"

    @property
    def resolved_ebay_token_url(self) -> str:
        return f"{self.resolved_ebay_base_url}/identity/v1/oauth2/token"

    @property
    def resolved_oauth_jwks_url(self) -> str | None:
        if self.auth_jwks_url:
            return self.auth_jwks_url.rstrip("/")
        authority = self.resolved_oauth_authority
        if not authority:
            return None
        return f"{authority}/discovery/v2.0/keys"

    def _normalize_oauth_resource_id(self, value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.strip()
        if cleaned.startswith("api://"):
            cleaned = cleaned[6:]
        if "/" in cleaned:
            cleaned = cleaned.rsplit("/", 1)[-1]
        return cleaned or None

    def resolved_oauth_audience(self) -> str | None:
        return self._normalize_oauth_resource_id(self.oauth_audience or self.oauth_client_id)

    def resolved_oauth_audience_variants(self) -> list[str]:
        audience = self.resolved_oauth_audience()
        if not audience:
            return []
        variants = [audience]
        api_uri = f"api://{audience}"
        if api_uri not in variants:
            variants.append(api_uri)
        if self.oauth_tenant_id:
            tenant_uri = f"api://{self.oauth_tenant_id.strip().strip('/')}/{audience}"
            if tenant_uri not in variants:
                variants.append(tenant_uri)
        return variants

    def resolved_oauth_scope(self) -> str | None:
        if self.oauth_scope:
            scope = self.oauth_scope.strip()
            if scope.endswith("/.default"):
                resource = self._normalize_oauth_resource_id(scope[: -len("/.default")])
                if resource:
                    return f"{resource}/.default"
            return scope
        audience = self.resolved_oauth_audience()
        if audience:
            return f"{audience}/.default"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
