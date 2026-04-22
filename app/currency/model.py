from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator


def _normalize_currency_code(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def _normalize_symbols(value: str | None) -> str | None:
    if value is None:
        return None
    parts = [part.strip().upper() for part in value.split(",") if part.strip()]
    if not parts:
        return None
    return ",".join(dict.fromkeys(parts))


def _validate_iso_date(value: str) -> str:
    date.fromisoformat(value)
    return value


class CurrencySymbolsArgs(BaseModel):
    pass


class CurrencyLatestArgs(BaseModel):
    base: str | None = None
    symbols: str | None = None

    @field_validator("base")
    @classmethod
    def normalize_base(cls, value: str | None) -> str | None:
        return _normalize_currency_code(value)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: str | None) -> str | None:
        return _normalize_symbols(value)


class CurrencyHistoryArgs(BaseModel):
    date: str
    base: str | None = None
    symbols: str | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_iso_date(value)

    @field_validator("base")
    @classmethod
    def normalize_base(cls, value: str | None) -> str | None:
        return _normalize_currency_code(value)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: str | None) -> str | None:
        return _normalize_symbols(value)


class CurrencyTimeseriesArgs(BaseModel):
    start_date: str
    end_date: str
    base: str | None = None
    symbols: str | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_dates(cls, value: str) -> str:
        return _validate_iso_date(value)

    @field_validator("base")
    @classmethod
    def normalize_base(cls, value: str | None) -> str | None:
        return _normalize_currency_code(value)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: str | None) -> str | None:
        return _normalize_symbols(value)

    @model_validator(mode="after")
    def validate_date_window(self) -> "CurrencyTimeseriesArgs":
        if self.start_date > self.end_date:
            raise ValueError("'start_date' must be on or before 'end_date'.")
        return self


class CurrencyConvertArgs(BaseModel):
    from_code: str = Field(alias="from")
    to: str
    amount: float = Field(gt=0)
    date: str | None = None

    @field_validator("from_code", "to")
    @classmethod
    def normalize_codes(cls, value: str) -> str:
        normalized = _normalize_currency_code(value)
        if normalized is None:
            raise ValueError("Currency codes must not be empty.")
        return normalized

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_iso_date(value)


class CurrencyFluctuationArgs(BaseModel):
    start_date: str
    end_date: str
    base: str | None = None
    symbols: str | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_dates(cls, value: str) -> str:
        return _validate_iso_date(value)

    @field_validator("base")
    @classmethod
    def normalize_base(cls, value: str | None) -> str | None:
        return _normalize_currency_code(value)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: str | None) -> str | None:
        return _normalize_symbols(value)

    @model_validator(mode="after")
    def validate_date_window(self) -> "CurrencyFluctuationArgs":
        if self.start_date > self.end_date:
            raise ValueError("'start_date' must be on or before 'end_date'.")
        return self
