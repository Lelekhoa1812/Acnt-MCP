from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class EbayItemSearchArgs(BaseModel):
    query: str | None = Field(None, description="Keyword phrase for eBay item discovery.")
    category_id: str | None = Field(None, description="Optional eBay category id to narrow search.")
    limit: int = Field(10, ge=1, le=200, description="Maximum number of listings to return, from 1 to 200.")
    offset: int = Field(0, ge=0, le=10_000, description="Offset into the result set.")

    @field_validator("query", "category_id")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _validate_scope(self) -> "EbayItemSearchArgs":
        if not self.query and not self.category_id:
            raise ValueError("Provide either 'query' or 'category_id' for an eBay search.")
        return self


class EbayItemDetailArgs(BaseModel):
    item_id: str = Field(description="RESTful eBay item id.")

    @field_validator("item_id")
    @classmethod
    def _strip_item_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("item_id must not be empty.")
        return cleaned


class EbayCategoryTreeArgs(BaseModel):
    category_tree_id: str = Field(description="eBay taxonomy category tree id.")

    @field_validator("category_tree_id")
    @classmethod
    def _strip_tree_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("category_tree_id must not be empty.")
        return cleaned
