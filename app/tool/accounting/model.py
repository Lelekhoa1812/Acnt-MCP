from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class _OpenCollectiveBaseArgs(BaseModel):
    slug: str = Field(description="Open Collective account slug.")
    limit: int = Field(20, ge=1, le=100, description="Maximum rows to return.")
    offset: int = Field(0, ge=0, le=10_000, description="Offset into the row set.")

    @field_validator("slug")
    @classmethod
    def _strip_slug(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("slug must not be empty.")
        return cleaned


class OpenCollectiveExpenseListArgs(_OpenCollectiveBaseArgs):
    search_term: str | None = Field(None, description="Optional expense search term.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class OpenCollectiveTransactionAllArgs(_OpenCollectiveBaseArgs):
    search_term: str | None = Field(None, description="Optional transaction search term.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class OpenCollectiveBudgetLookupArgs(_OpenCollectiveBaseArgs):
    @model_validator(mode="after")
    def _validate_slug(self) -> "OpenCollectiveBudgetLookupArgs":
        return self
