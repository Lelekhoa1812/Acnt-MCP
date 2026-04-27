from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class WeatherResolveArgs(BaseModel):
    query: str | None = Field(None, description="Place name to geocode, e.g. Melbourne, AU.")
    lat: float | None = Field(None, description="Latitude for direct coordinate lookup.")
    lon: float | None = Field(None, description="Longitude for direct coordinate lookup.")
    limit: int = Field(5, ge=1, le=10, description="Maximum geocoding candidates to return, from 1 to 10.")

    @model_validator(mode="after")
    def validate_location(self) -> "WeatherResolveArgs":
        if self.query:
            return self
        if self.lat is None or self.lon is None:
            raise ValueError("Provide either 'query' or both 'lat' and 'lon'.")
        return self


class WeatherCurrentArgs(WeatherResolveArgs):
    units: str | None = Field(None, description="OpenWeather units, e.g. metric, imperial, or standard.")
    lang: str | None = Field(None, description="Optional OpenWeather language code for descriptions.")


class WeatherForecastArgs(WeatherResolveArgs):
    units: str | None = Field(None, description="OpenWeather units, e.g. metric, imperial, or standard.")
    lang: str | None = Field(None, description="Optional OpenWeather language code for descriptions.")
    count: int = Field(8, ge=1, le=40, description="Number of 3-hour forecast points to return, from 1 to 40.")


class WeatherHistoryArgs(WeatherResolveArgs):
    date: str | None = Field(None, description="Historical date in YYYY-MM-DD format when supported by the configured endpoint.")
    start: str | None = Field(None, description="Start date/time for a historical window when supported.")
    end: str | None = Field(None, description="End date/time for a historical window when supported.")
    dt: int | None = Field(None, description="Unix timestamp for historical conditions when supported.")
    onlyCurrent: bool = Field(True, description="Return a compact current-like historical point when possible.")
    units: str | None = Field(None, description="OpenWeather units, e.g. metric, imperial, or standard.")
    lang: str | None = Field(None, description="Optional OpenWeather language code for descriptions.")

    @model_validator(mode="after")
    def validate_window(self) -> "WeatherHistoryArgs":
        if any([self.date, self.start, self.end, self.dt]):
            return self
        raise ValueError("Provide at least one of 'date', 'start', 'end', or 'dt' for weather history.")
