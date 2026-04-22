from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class WeatherResolveArgs(BaseModel):
    query: str | None = None
    lat: float | None = None
    lon: float | None = None
    limit: int = Field(5, ge=1, le=10)

    @model_validator(mode="after")
    def validate_location(self) -> "WeatherResolveArgs":
        if self.query:
            return self
        if self.lat is None or self.lon is None:
            raise ValueError("Provide either 'query' or both 'lat' and 'lon'.")
        return self


class WeatherCurrentArgs(WeatherResolveArgs):
    units: str | None = None
    lang: str | None = None


class WeatherForecastArgs(WeatherResolveArgs):
    units: str | None = None
    lang: str | None = None
    count: int = Field(8, ge=1, le=40)


class WeatherHistoryArgs(WeatherResolveArgs):
    date: str | None = None
    start: str | None = None
    end: str | None = None
    dt: int | None = None
    onlyCurrent: bool = True
    units: str | None = None
    lang: str | None = None

    @model_validator(mode="after")
    def validate_window(self) -> "WeatherHistoryArgs":
        if any([self.date, self.start, self.end, self.dt]):
            return self
        raise ValueError("Provide at least one of 'date', 'start', 'end', or 'dt' for weather history.")
