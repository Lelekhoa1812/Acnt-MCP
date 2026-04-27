from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class NewsSearchArgs(BaseModel):
    q: str | None = Field(None, description="Keyword or phrase for broad article search.")
    searchIn: str | None = Field(None, description="Comma-separated fields to search, such as title,description,content.")
    sources: str | None = Field(None, description="Comma-separated News API source IDs; cannot be guessed without news_sources.")
    domains: str | None = Field(None, description="Comma-separated domains to include, e.g. bbc.co.uk.")
    excludeDomains: str | None = Field(None, description="Comma-separated domains to exclude.")
    from_date: str | None = Field(None, alias="from", description="Oldest publication date/time in ISO 8601 format.")
    to: str | None = Field(None, description="Newest publication date/time in ISO 8601 format.")
    language: str | None = Field(None, description="Two-letter language code supported by News API, e.g. en.")
    sortBy: str | None = Field(None, description="News API sort order such as relevancy, popularity, or publishedAt.")
    pageSize: int = Field(10, ge=1, le=100, description="Number of articles to return, from 1 to 100.")
    page: int = Field(1, ge=1, description="Article result page.")

    @model_validator(mode="after")
    def validate_query(self) -> "NewsSearchArgs":
        if not any([self.q, self.sources, self.domains]):
            raise ValueError("At least one of 'q', 'sources', or 'domains' must be provided.")
        return self


class NewsHeadlinesArgs(BaseModel):
    q: str | None = Field(None, description="Optional keyword for top-headline filtering.")
    country: str | None = Field(None, description="Two-letter country code for regional headlines, e.g. au.")
    category: str | None = Field(None, description="News API headline category such as business, technology, or sports.")
    sources: str | None = Field(None, description="Comma-separated source IDs; do not combine with country or category.")
    pageSize: int = Field(10, ge=1, le=100, description="Number of headlines to return, from 1 to 100.")
    page: int = Field(1, ge=1, description="Headline result page.")

    @model_validator(mode="after")
    def validate_scope(self) -> "NewsHeadlinesArgs":
        if not any([self.q, self.country, self.category, self.sources]):
            raise ValueError("Provide at least one of 'q', 'country', 'category', or 'sources'.")
        if self.sources and (self.country or self.category):
            raise ValueError("'sources' cannot be combined with 'country' or 'category' for top headlines.")
        return self


class NewsSourcesArgs(BaseModel):
    category: str | None = Field(None, description="Optional News API source category filter.")
    language: str | None = Field(None, description="Optional two-letter source language code, e.g. en.")
    country: str | None = Field(None, description="Optional two-letter source country code, e.g. au.")
