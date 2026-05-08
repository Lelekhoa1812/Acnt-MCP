from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator


class OpenLibraryBookSearchArgs(BaseModel):
    query: str | None = Field(None, description="Search terms for Open Library.")
    title: str | None = Field(None, description="Optional title filter.")
    author: str | None = Field(None, description="Optional author filter.")
    subject: str | None = Field(None, description="Optional subject filter.")
    limit: int = Field(10, ge=1, le=100, description="Number of books to return.")
    page: int = Field(1, ge=1, description="Search result page.")

    @field_validator("query", "title", "author", "subject")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _validate_search_scope(self) -> "OpenLibraryBookSearchArgs":
        if not any([self.query, self.title, self.author, self.subject]):
            raise ValueError("Provide at least one of 'query', 'title', 'author', or 'subject'.")
        return self


class OpenLibraryIsbnLookupArgs(BaseModel):
    isbn: str = Field(description="ISBN-10 or ISBN-13 to look up.")

    @field_validator("isbn")
    @classmethod
    def _normalize_isbn(cls, value: str) -> str:
        cleaned = re.sub(r"[\s-]+", "", value.strip()).upper()
        if not cleaned:
            raise ValueError("isbn must not be empty.")
        return cleaned


class OpenLibrarySubjectListArgs(BaseModel):
    subject: str = Field(description="Subject to browse in Open Library.")
    limit: int = Field(10, ge=1, le=100, description="Number of works to return.")
    page: int = Field(1, ge=1, description="Subject result page.")

    @field_validator("subject")
    @classmethod
    def _normalize_subject(cls, value: str) -> str:
        cleaned = value.strip().lower().replace(" ", "_")
        if not cleaned:
            raise ValueError("subject must not be empty.")
        return cleaned
