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
    server_version: str = Field("1.0.0", alias="HTH_SERVER_VERSION")
    transport: str = Field("http", alias="HTH_TRANSPORT")
    port: int = Field(3000, alias="HTH_PORT")
    api_prefix: str = Field("/api/v1", alias="HTH_API_PREFIX")
    log_level: str = Field("debug", alias="HTH_LOG_LEVEL")
    data_source: str = Field("harmonise", alias="HTH_DATA_SOURCE")
    local_harmonise: bool = Field(False, alias="LOCAL_HARMONISE")
    local_harmonise_endpoint: str = Field("http://localhost:9000", alias="LOCAL_HARMONISE_ENDPOINT")
    cloud_harmonise_endpoint: str = Field("http://localhost:9000", alias="CLOUD_HARMONISE_ENDPOINT")
    cloud_harmonise_api: str | None = Field(None, alias="CLOUD_HARMONISE_API")
    cloud_harmonise_image: str | None = Field(None, alias="CLOUD_HARMONISE_IMAGE")
    harmonise_headers: str = Field("{}", alias="HTH_HARMONISE_HEADERS")
    harmonise_timeout_ms: int = Field(10000, alias="HTH_HARMONISE_TIMEOUT_MS")
    redis_url: str = Field("redis://localhost:6379", alias="HTH_REDIS_URL")
    cache_ttl_seconds: int = Field(300, alias="HTH_CACHE_TTL_SECONDS")
    session_ttl_seconds: int = Field(1800, alias="HTH_SESSION_TTL_SECONDS")
    default_session_id: str = Field("default", alias="HTH_DEFAULT_SESSION_ID")
    mock_catalog_path: str = Field("./mock/product-catalog-enriched.json", alias="HTH_MOCK_CATALOG_PATH")
    mock_details_path: str = Field("./mock/product-details-enriched.json", alias="HTH_MOCK_DETAILS_PATH")
    mock_departments_path: str = Field("./mock/departments.json", alias="HTH_MOCK_DEPARTMENTS_PATH")
    mock_categories_path: str = Field("./mock/categories.json", alias="HTH_MOCK_CATEGORIES_PATH")
    enable_mock_ui_simulation: bool = Field(True, alias="HTH_ENABLE_MOCK_UI_SIMULATION")
    mock_ui_path: str = Field("./ui/mock/index.html", alias="HTH_MOCK_UI_PATH")
    # Motivation vs Logic: default false so session/tool caches use Redis as the
    # durable source of truth; set HTH_REDIS_FALLBACK_ENABLED=true for dev without Redis.
    redis_fallback_enabled: bool = Field(False, alias="HTH_REDIS_FALLBACK_ENABLED")
    # Motivation vs Logic: local REST `/query` runs do not get Claude-managed
    # browser memory, so we keep a short device-local conversation buffer in
    # process memory (never Redis) to preserve follow-up context while developing.
    local_chat_memory_enabled: bool = Field(True, alias="HTH_LOCAL_CHAT_MEMORY_ENABLED")
    local_chat_memory_turns: int = Field(6, ge=1, alias="HTH_LOCAL_CHAT_MEMORY_TURNS")
    agent_max_steps: int = Field(8, alias="HTH_AGENT_MAX_STEPS")
    # Motivation vs Logic: broad inventory answers can legitimately need large
    # Markdown tables, so the agent completion budget is configurable instead of
    # hard-coded to a small single-paragraph default.
    agent_completion_tokens: int = Field(3600, alias="HTH_AGENT_COMPLETION_TOKENS")
    foundry_endpoint: str | None = Field(None, alias="AZURE_AI_FOUNDRY_ENDPOINT")
    foundry_api_key: str | None = Field(None, alias="AZURE_AI_FOUNDRY_API_KEY")
    foundry_model: str = Field("gpt-5.4-mini", alias="AZURE_AI_FOUNDRY_MODEL")
    foundry_slm_model: str | None = Field(None, alias="AZURE_AI_FOUNDRY_SLM")
    foundry_timeout_ms: int = Field(60000, alias="AZURE_AI_FOUNDRY_TIMEOUT_MS")
    exchange_rate_api_key: str | None = Field(None, alias="EXCHANGE_RATE_API")
    open_weather_api_key: str | None = Field(None, alias="OPEN_WEATHER_API")
    news_api_key: str | None = Field(None, alias="NEWS_API")

    @property
    def harmonise_timeout_seconds(self) -> float:
        return self.harmonise_timeout_ms / 1000

    @property
    def foundry_timeout_seconds(self) -> float:
        return self.foundry_timeout_ms / 1000

    @property
    def project_root(self) -> Path:
        return Path.cwd()

    @property
    def harmonise_mode(self) -> str:
        return "local" if self.local_harmonise else "remote"

    @property
    def harmonise_base_url(self) -> str:
        return self.local_harmonise_endpoint if self.local_harmonise else self.cloud_harmonise_endpoint

    @property
    def data_source_label(self) -> str:
        return f"harmonise_{self.harmonise_mode}"

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
        return notes


@lru_cache
def get_settings() -> Settings:
    return Settings()
