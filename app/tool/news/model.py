from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class NewsSearchArgs(BaseModel):
    q: str | None = None
    searchIn: str | None = None
    sources: str | None = None
    domains: str | None = None
    excludeDomains: str | None = None
    from_date: str | None = Field(None, alias="from")
    to: str | None = None
    language: str | None = None
    sortBy: str | None = None
    pageSize: int = Field(10, ge=1, le=100)
    page: int = Field(1, ge=1)

    @model_validator(mode="after")
    def validate_query(self) -> "NewsSearchArgs":
        if not any([self.q, self.sources, self.domains]):
            raise ValueError("At least one of 'q', 'sources', or 'domains' must be provided.")
        return self


class NewsHeadlinesArgs(BaseModel):
    q: str | None = None
    country: str | None = None
    category: str | None = None
    sources: str | None = None
    pageSize: int = Field(10, ge=1, le=100)
    page: int = Field(1, ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "NewsHeadlinesArgs":
        if not any([self.q, self.country, self.category, self.sources]):
            raise ValueError("Provide at least one of 'q', 'country', 'category', or 'sources'.")
        if self.sources and (self.country or self.category):
            raise ValueError("'sources' cannot be combined with 'country' or 'category' for top headlines.")
        return self


class NewsSourcesArgs(BaseModel):
    category: str | None = None
    language: str | None = None
    country: str | None = None
