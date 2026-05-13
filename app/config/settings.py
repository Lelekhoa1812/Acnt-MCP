from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    server_name: str = Field("acnt-mcp", alias="ACNT_SERVER_NAME")
    server_version: str = Field("2.0.0", alias="ACNT_SERVER_VERSION")
    transport: str = Field("http", alias="ACNT_TRANSPORT")
    port: int = Field(80, alias="ACNT_PORT")
    api_prefix: str = Field("/api/v1", alias="ACNT_API_PREFIX")
    log_level: str = Field("info", alias="ACNT_LOG_LEVEL")
    public_base_url: str | None = Field(None, alias="ACNT_PUBLIC_BASE_URL")

    cache_ttl_seconds: int = Field(300, alias="ACNT_CACHE_TTL_SECONDS")

    exchange_rate_api_key: str | None = Field(None, alias="EXCHANGE_RATE_API")

    opencollective_graphql_url: str = Field(
        "https://api.opencollective.com/graphql/v2",
        alias="OPENCOLLECTIVE_GRAPHQL_URL",
    )
    opencollective_pat_token: str | None = Field(None, alias="OPENCOLLECTIVE_PAT_TOKEN")
    opencollective_client_id: str | None = Field(None, alias="OPENCOLLECTIVE_CLIENT_ID")
    opencollective_client_secret: str | None = Field(None, alias="OPENCOLLECTIVE_CLIENT_SECRET")

    mcp_path: str = Field("/mcp", alias="ACNT_MCP_PATH")
    mcp_stateless: bool = Field(False, alias="ACNT_MCP_STATELESS")
    mcp_json_response: bool = Field(False, alias="ACNT_MCP_JSON_RESPONSE")
    mcp_retry_interval_ms: int | None = Field(2500, ge=0, alias="ACNT_MCP_RETRY_INTERVAL_MS")
    mcp_session_idle_timeout_seconds: float | None = Field(
        1800.0, ge=0, alias="ACNT_MCP_SESSION_IDLE_TIMEOUT_SECONDS"
    )
    mcp_compact_envelope: bool = Field(True, alias="ACNT_MCP_COMPACT_ENVELOPE")
    mcp_allowed_hosts: str | None = Field(None, alias="ACNT_MCP_ALLOWED_HOSTS")
    mcp_allowed_origins: str | None = Field(None, alias="ACNT_MCP_ALLOWED_ORIGINS")

    @property
    def project_root(self) -> Path:
        return Path.cwd()

    def resolve_path(self, relative: str | Path) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute():
            return candidate
        return self.project_root / candidate

    @property
    def parsed_mcp_allowed_hosts(self) -> list[str]:
        return _split_csv(self.mcp_allowed_hosts)

    @property
    def parsed_mcp_allowed_origins(self) -> list[str]:
        return _split_csv(self.mcp_allowed_origins)

    def startup_notes(self) -> list[str]:
        notes: list[str] = []
        if not self.exchange_rate_api_key:
            notes.append("EXCHANGE_RATE_API is not configured; fx_* tools will fail at runtime.")
        if not self.opencollective_pat_token:
            notes.append("OPENCOLLECTIVE_PAT_TOKEN is not configured; accounting_* tools will fail at runtime.")
        return notes


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
